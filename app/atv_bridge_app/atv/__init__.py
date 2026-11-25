"""The only module that depends on pyatv, including the pinned 0.18 compatibility hooks."""

import asyncio
import hashlib
import importlib
import re
from contextlib import suppress
from functools import partial, wraps

import pyatv
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pyatv import exceptions
from pyatv.auth.hap_pairing import parse_credentials
from pyatv.auth.hap_tlv8 import TlvValue, read_tlv
from pyatv.const import FeatureName, FeatureState, KeyboardFocusState, Protocol
from pyatv.core import CoreStateDispatcher, create_core
from pyatv.core.facade import FacadeAppleTV
from pyatv.protocols import PROTOCOLS
from pyatv.protocols.airplay.auth import hap, pair_verify
from pyatv.protocols.companion.api import HidCommand, MediaControlCommand
from pyatv.protocols.companion.connection import FrameType
from pyatv.protocols.companion.protocol import CompanionProtocol
from pyatv.protocols.raop.protocols import StreamProtocol
from pyatv.storage.memory_storage import MemoryStorage
from pyatv.support.http import create_session, http_connect
from pyatv.support.net import tcp_keepalive
from pyatv.support.rtsp import RtspSession

from ..state import CONTROLLER_NAME, Fault
from .discovery import Resolver, unreachable


def optional_module(name):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if name == exc.name or name.startswith(exc.name + "."):
            return None
        raise


extras = optional_module("pyatv.protocols.companion.extras")
converter = optional_module("pyatv.auth.convert")


def check_reply(reply):
    for key in ("_em", "_ec", "_eu"):
        if key in reply:
            raise exceptions.ProtocolError(f"Companion rejected command ({key}={reply[key]})")
    return reply


def install_reply_check():
    original = CompanionProtocol._exchange_generic_opack
    if getattr(original, "_bridge_checked", False):
        return

    @wraps(original)
    async def checked(self, *args, **kwargs):
        return check_reply(await original(self, *args, **kwargs))

    checked._bridge_checked = True
    CompanionProtocol._exchange_generic_opack = checked


def install_sender_identity():
    # The pinned stock library uses these headers for HAP setup and verify.
    # The fork copies them and accepts a per-pairing display name as well.
    hap._AIRPLAY_HEADERS["X-Apple-Client-Name"] = CONTROLLER_NAME
    original = RtspSession.setup
    if getattr(original, "_bridge_named", False):
        return

    @wraps(original)
    async def named_setup(self, headers=None, body=None):
        if isinstance(body, dict) and "name" in body:
            # pyatv hardcodes "pyatv" in the RAOP/AirPlay v2 base SETUP.
            body = body | {"name": CONTROLLER_NAME}
        return await original(
            self, headers=(headers or {}) | {"X-Apple-Client-Name": CONTROLLER_NAME}, body=body
        )

    named_setup._bridge_named = True
    RtspSession.setup = named_setup


def credentials(identity, receiver):
    return ":".join(
        (
            receiver["ltpk"],
            identity["ed25519_seed"],
            receiver["atv_id"],
            identity["controller_id"].encode().hex(),
        )
    )


def tvpipe_credentials(identity, receiver, host, port):
    if receiver and converter and hasattr(converter, "to_tvpipe"):
        return converter.to_tvpipe(
            credentials(identity, receiver), name="Apple TV", addr=f"{host}:{port}"
        )
    text = f"controller = {identity['controller_id']}\nseed = {identity['ed25519_seed']}\n"
    if receiver:
        text += (
            f"\nreceiver = {receiver['atv_id']}\npk = {receiver['ltpk']}\n"
            f"name = Apple TV\naddr = {host}:{port}\n"
        )
    return text


# Descriptive now-playing fields beyond those status() and media_details() name. This is an
# allowlist: URLs, profile and subscription identifiers and binary blobs are never copied.
RAW_KEEP = {
    "subtitle", "albumArtistName", "directorName", "composer", "localizedContentRating",
    "isExplicitItem", "releaseDate", "albumYear", "trackNumber", "totalTrackCount", "discNumber",
    "totalDiscCount", "radioStationName", "mediaSubType", "episodeType", "isAlwaysLive",
    "iTunesStoreArtistIdentifier", "iTunesStoreAlbumIdentifier", "serviceIdentifier", "brandIdentifier",
}


def raw_metadata(metadata):
    """The allowed extra fields set in the MRP ContentItemMetadata message, as JSON values."""
    raw = {}
    for field, value in metadata.ListFields():
        if field.name not in RAW_KEEP:
            continue
        if field.enum_type:
            raw[field.name] = field.enum_type.values_by_number[value].name
        elif isinstance(value, str):
            # An allowed field can still carry a link. Links can hold account tokens.
            if "://" not in value:
                raw[field.name] = value[:200]
        elif not isinstance(value, float) or value == value:
            raw[field.name] = value
    return raw


class Adapter:
    supports_play_url = hasattr(StreamProtocol, "supports_playback_state")

    def __init__(self, host, state, identifier=""):
        install_reply_check()
        install_sender_identity()
        self.resolver = Resolver(host, state, identifier)
        self.state = state
        self.access_restricted = None

    @property
    def host(self):
        return self.resolver.address

    async def with_receiver(self, operation, selected=None):
        failed_host = None
        for attempt in range(2):
            config, source = await self.resolver.resolve(selected, failed_host)
            airplay = config.get_service(Protocol.AirPlay)
            self.access_restricted = bool(airplay and airplay.properties.get("acl") == "1")
            try:
                return await operation(config), config, source
            except (
                OSError,
                asyncio.IncompleteReadError,
                exceptions.ConnectionFailedError,
                exceptions.ConnectionLostError,
                exceptions.OperationTimeoutError,
            ) as exc:
                if attempt:
                    raise unreachable() from exc
                failed_host = str(config.address)

    async def connect(self, on_power, companion_record=None, companion_only=False):
        record = companion_record or self.state.receiver("companion")
        if not record:
            if not self.state.pairings.get("airplay"):
                raise Fault(
                    "pairing_required", "AirPlay is unpaired.", "Pair AirPlay in Home Assistant."
                )
            raise Fault(
                "pairing_required", "Companion verification failed.", "Complete Companion pairing."
            )
        if not companion_record and not self.state.pairings.get("companion"):
            # An imported AirPlay-only record, or an interrupted verification.
            # Probe Companion alone so a failure from another protocol cannot
            # trigger a separate PIN. Network failures remain reconnectable.
            try:
                await self.verify_reuse("companion", record)
            except (exceptions.AuthenticationError, exceptions.InvalidCredentialsError):
                self.state.save_pairings(
                    credential_reuse=self.state.pairings.get("credential_reuse", {})
                    | {"companion": False}
                )
                raise Fault(
                    "pairing_required",
                    "Companion rejected the AirPlay credentials.",
                    "Complete Companion pairing in Home Assistant.",
                ) from None
            self.state.save_pairings(
                companion=record,
                credential_reuse=self.state.pairings.get("credential_reuse", {}) | {"companion": True},
            )

        open_session = partial(
            self._connect, on_power=on_power, record=record, companion_only=companion_only
        )
        session, config, source = await self.with_receiver(open_session)
        try:
            self.resolver.remember(config, source)
        except BaseException:
            await session.close()
            raise
        return session

    async def _connect(self, config, on_power, record, companion_only):
        storage = MemoryStorage()
        settings = await storage.get_settings(config)
        identity = self.state.identity
        digest = bytearray(hashlib.sha256(identity["controller_id"].encode()).digest()[:6])
        digest[0] = (digest[0] | 2) & 254
        settings.info.mac = settings.info.device_id = digest.hex(":").upper()
        settings.info.rp_id = digest.hex()
        settings.info.name = identity["name"]
        companion = settings.protocols.companion
        # Forks that own the heartbeat should not also receive one from the supervisor.
        for name, value in (("keepalive_interval", 15), ("tcp_keepalive", True)):
            if hasattr(companion, name):
                setattr(companion, name, value)
        for protocol in (Protocol.Companion, Protocol.AirPlay, Protocol.RAOP):
            receiver = (
                record if protocol == Protocol.Companion else self.state.receiver(protocol.name.lower())
            )
            if receiver and config.get_service(protocol):
                value = credentials(identity, receiver)
                if (
                    protocol == Protocol.Companion
                    and converter
                    and hasattr(converter, "companion_credentials")
                ):
                    value = converter.companion_credentials(value)
                config.set_credentials(protocol, value)
        # Keep the facade in hand while protocols connect. pyatv.connect in 0.18
        # only closes its HTTP session when a later protocol fails during startup.
        manager = await create_session()
        dispatcher = CoreStateDispatcher()
        facade = FacadeAppleTV(config, manager, dispatcher, settings)
        pending = []
        try:
            for protocol, methods in PROTOCOLS.items():
                service = config.get_service(protocol)
                if (
                    not service
                    or not service.enabled
                    or (companion_only and protocol != Protocol.Companion)
                ):
                    continue
                if protocol == Protocol.AirPlay and not service.credentials:
                    continue
                core = await create_core(
                    config,
                    service,
                    settings=settings,
                    device_listener=facade,
                    session_manager=manager,
                    core_dispatcher=dispatcher,
                    takeover_method=partial(facade.takeover, protocol),
                )
                for setup in methods.setup(core):
                    pending.append(setup)
                    facade.add_protocol(setup)
            await facade.connect()
            return Session(facade, on_power)
        except BaseException:
            tasks = set()
            if facade._pending_tasks is None:
                for setup in pending:
                    if setup.protocol not in facade._protocol_handlers:
                        tasks.update(setup.close())
            tasks.update(facade.close())
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def begin_pair(self, protocol, selected=None):
        handler, config, source = await self.with_receiver(
            partial(self._begin, protocol=protocol), selected
        )
        handler.bridge_receiver = (config, source, selected)
        return handler

    async def _begin(self, config, protocol):
        if self.access_restricted and protocol == "airplay":
            raise Fault(
                "allow_access_changed",
                "AirPlay advertises home-only access.",
                "Set AirPlay and HomeKit → Allow Access to Anyone on the Same Network.",
            )
        storage = MemoryStorage()
        settings = await storage.get_settings(config)
        settings.info.name = CONTROLLER_NAME
        handler = await pyatv.pair(
            config,
            getattr(Protocol, {"airplay": "AirPlay", "companion": "Companion"}[protocol]),
            asyncio.get_running_loop(),
            name=CONTROLLER_NAME,
            storage=storage,
        )
        try:
            await handler.begin()
            # Both HAP procedures initialize a fresh ephemeral X25519 exchange
            # in begin(). Install our persistent signing material before SRP M3.
            srp = handler.pairing_procedure.srp
            seed = bytes.fromhex(self.state.identity["ed25519_seed"])
            srp.pairing_id = self.state.identity["controller_id"].encode()
            srp._signing_key = Ed25519PrivateKey.from_private_bytes(seed)
            srp._auth_private = seed
            srp._auth_public = srp._signing_key.public_key().public_bytes_raw()
            return handler
        except BaseException:
            await handler.close()
            raise

    async def finish_pair(self, handler, pin):
        handler.pin(int(pin))
        await handler.finish()
        creds = parse_credentials(handler.service.credentials)
        if (
            creds.ltsk.hex() != self.state.identity["ed25519_seed"]
            or creds.client_id.decode() != self.state.identity["controller_id"]
        ):
            raise RuntimeError("Paired controller identity mismatch")
        return {"ltpk": creds.ltpk.hex(), "atv_id": creds.atv_id.hex()}

    async def verify_reuse(self, protocol, record):
        if protocol == "companion":
            session = await self.connect(lambda *_: None, record, companion_only=True)
            # A successful authenticated connection proves reuse, even if
            # this tvOS version restricts the app-list command.
            await session.close()
        else:
            _, config, source = await self.with_receiver(partial(self._verify_raop, record=record))
            self.resolver.remember(config, source)

    async def _verify_raop(self, config, record):
        service = config.get_service(Protocol.RAOP)
        if not service:
            raise ConnectionError("No RAOP endpoint advertised")
        conn = await http_connect(str(config.address), service.port)
        try:
            verifier = pair_verify(parse_credentials(credentials(self.state.identity, record)), conn)
            # Stock 0.18 ignores a TLV error in the final HTTP 200 reply.
            send = verifier._send

            async def checked_send(fields):
                response = await send(fields)
                tlv = read_tlv(response.body) if isinstance(response.body, bytes) else {}
                if response.code != 200 or TlvValue.Error in tlv:
                    raise exceptions.AuthenticationError("RAOP rejected pair-verify")
                if fields[TlvValue.SeqNo] == b"\x03" and tlv.get(TlvValue.SeqNo) != b"\x04":
                    raise exceptions.InvalidResponseError("Missing RAOP pair-verify M4")
                return response

            verifier._send = checked_send
            if not await verifier.verify_credentials():
                raise exceptions.AuthenticationError("RAOP did not verify")
        finally:
            conn.close()


PLAYBACK_FEATURES = {
    "seek_absolute": FeatureName.SetPosition,
    "skip_forward": FeatureName.SkipForward,
    "skip_backward": FeatureName.SkipBackward,
    "stop": FeatureName.Stop,
    "set_volume": FeatureName.SetVolume,
}


class Session:
    def __init__(self, facade, on_power):
        self.stream_protocol = None
        self.volume_sample = (0.0, None)
        self.facade = facade
        self.api = facade.apps.get(Protocol.Companion).api
        self.on_power = on_power
        self.fork_heartbeat = hasattr(facade.settings.protocols.companion, "keepalive_interval")
        self.extras = (
            extras.CompanionExtras(facade) if extras and hasattr(extras, "CompanionExtras") else None
        )
        facade.power.listener = self
        self.attention = "unknown"
        self.api.listen_to("TVSystemStatus", self._attention)
        self.api.listen_to("SystemStatus", self._attention)
        conn = self.api._connection
        sock = conn.transport.get_extra_info("socket")
        self.keepalive_timer = None
        if not self.fork_heartbeat:
            if sock:
                tcp_keepalive(sock)
            self.keepalive_timer = asyncio.get_running_loop().call_later(15, self._send_noop)

    async def _attention(self, data):
        previous = self.attention
        self.attention = {1: "off", 2: "screensaver", 3: "on", 4: "on"}.get(
            data.get("state"), "unknown"
        )
        self.on_power(previous, self.attention)

    def powerstate_update(self, old_state, new_state):
        self.on_power(old_state.name.lower(), new_state.name.lower())

    async def power(self):
        return (
            self.attention
            if self.attention != "unknown"
            else self.facade.power.power_state.name.lower()
        )

    def _send_noop(self):
        # Keep the upstream 0.18 session alive even while an agent holds a key
        # or waits for a long command sequence under the engine lock.
        self.keepalive_timer = None
        conn = self.api._connection
        if conn is None or not conn.connected:
            return
        try:
            conn.send(FrameType.NoOp, b"")
        except (OSError, RuntimeError, exceptions.InvalidStateError):
            conn.close()
            return
        self.keepalive_timer = asyncio.get_running_loop().call_later(15, self._send_noop)

    async def close(self):
        if self.keepalive_timer is not None:
            self.keepalive_timer.cancel()
        await asyncio.gather(*self.facade.close(), return_exceptions=True)

    async def play_url(self, url, position):
        stream = self.facade.stream.get(Protocol.AirPlay)
        original = stream.create_airplay_protocol

        def track(*args):
            self.stream_protocol = original(*args)
            return self.stream_protocol

        stream.create_airplay_protocol = track
        try:
            await self.facade.stream.play_url(url, position=position)
            # A socket close makes the fork's wait return "stopped"; the last
            # receiver event remains "playing". A normal EOF sends idle/stopped.
            return getattr(self.stream_protocol, "playback_state", None) in ("playing", "paused")
        except exceptions.PlaybackError as exc:
            # The fork reports receiver-requested (FCUP) fetch failures here.
            # A media-origin 403 is independent of the AirPlay pairing.
            reason = getattr(self.stream_protocol, "url_load_error", None)
            if not reason:
                raise
            status = re.search(r" returned HTTP ([0-9]{3})$", reason)
            message = (f"The media server returned HTTP {status[1]}." if status
                       else "The media URL could not be fetched for the TV.")
            raise Fault("page_unreachable", message,
                        "Use a direct media URL that the Apple TV can fetch.") from exc
        finally:
            stream.create_airplay_protocol = original
            self.stream_protocol = None

    async def swipe(self, direction):
        points = {
            "left": (850, 500, 150, 500),
            "right": (150, 500, 850, 500),
            "down": (500, 150, 500, 850),
            "up": (500, 850, 500, 150),
        }
        await self.facade.touch.swipe(*points[direction], 300)

    async def apps(self):
        return [
            {"bundle_id": app.identifier, "name": app.name, "allowed": True}
            for app in await self.facade.apps.app_list()
        ]

    async def open(self, target):
        await self.api.launch_app(target)

    def control_protocol(self, feature):
        # pyatv's facade prefers MRP even when only Companion advertises this action.
        protocols = (Protocol.Companion, Protocol.MRP) if feature == FeatureName.SetVolume else (Protocol.MRP, Protocol.Companion)
        for protocol in protocols:
            features = self.facade.features.get(protocol)
            if features and features.in_state(FeatureState.Available, feature):
                return protocol
        return None

    def control_interface(self, name, interface):
        protocol = self.control_protocol(PLAYBACK_FEATURES[name])
        if protocol is None:
            raise Fault("unsupported_action", f"The current player or output does not support {name}.",
                        "Check tv_status for available controls.")
        return interface.get(protocol)

    async def seek(self, position_s, offset_s):
        remote = self.facade.remote_control
        if position_s is not None:
            await self.control_interface("seek_absolute", remote).set_position(position_s)
        elif offset_s > 0:
            await self.control_interface("skip_forward", remote).skip_forward(offset_s)
        else:
            await self.control_interface("skip_backward", remote).skip_backward(-offset_s)

    async def read_companion_volume(self, *, fresh=False):
        clock = asyncio.get_running_loop().time
        sampled, level = self.volume_sample
        if fresh or clock() - sampled >= 2:
            async with asyncio.timeout(2):
                reply = await self.api.mediacontrol_command(MediaControlCommand.GetVolume)
            fraction = reply.get("_c", {}).get("_vol")
            level = round(fraction * 100, 2) if isinstance(fraction, (int, float)) and 0 <= fraction <= 1 else None
            self.volume_sample = (clock(), level)
        return level

    async def set_volume(self, percent):
        audio = self.control_interface("set_volume", self.facade.audio)
        if audio is self.facade.audio.get(Protocol.Companion):
            # A receiver may acknowledge SetVolume without pushing a new volume event.
            # GetVolume supplies direct readback even when the output quantizes the level.
            await self.api.mediacontrol_command(MediaControlCommand.SetVolume, {"_vol": percent / 100})
            for attempt in range(6):
                if attempt:
                    await asyncio.sleep(0.2)
                observed = await self.read_companion_volume(fresh=True)
                if observed is not None and abs(observed - percent) <= 0.5:
                    break
            return observed
        await audio.set_volume(percent)
        return audio.volume

    async def hid(self, key, hold_ms):
        if key == "stop":
            await self.control_interface("stop", self.facade.remote_control).stop()
            return
        media = {
            "play": MediaControlCommand.Play,
            "pause": MediaControlCommand.Pause,
            "next": MediaControlCommand.NextTrack,
            "previous": MediaControlCommand.PreviousTrack,
        }
        if key in media:
            await self.api.mediacontrol_command(media[key])
            return
        if key == "siri" and self.extras and not hold_ms:
            await self.extras.siri_press()
            return
        command = HidCommand[key.title().replace("_", "")]
        if key in ("wake", "sleep"):
            # CompanionPower sends a HID release for these system commands.
            await self.api.hid_command(False, command)
            return
        await self.api.hid_command(True, command)
        try:
            if hold_ms:
                await asyncio.sleep(hold_ms / 1000)
        finally:
            await self.api.hid_command(False, command)

    async def type(self, text, clear):
        keyboard = self.facade.keyboard
        if keyboard.text_focus_state != KeyboardFocusState.Focused:
            raise Fault("keyboard_unfocused", "No keyboard has focus.", "Select a text field first.")
        previous = "" if clear else await keyboard.text_get()
        if clear:
            await keyboard.text_set(text)
        else:
            await keyboard.text_append(text)
        actual = await keyboard.text_get()
        return actual is not None and actual == (previous or "") + text

    async def media_details(self):
        """Now-playing fields beyond status(). Reads pyatv's cached push state and sends nothing."""
        details = {}
        with suppress(exceptions.NotSupportedError):
            media = await self.facade.metadata.playing()
            details = {
                "media_type": media.media_type.name.lower(),
                "album": media.album,
                "genre": media.genre,
                "series_name": media.series_name,
                "season_number": media.season_number,
                "episode_number": media.episode_number,
                "itunes_store_identifier": media.itunes_store_identifier,
            }
        mrp = self.facade.metadata.get(Protocol.MRP)
        player = getattr(getattr(mrp, "psm", None), "playing", None)
        if player is not None:
            # Playing.hash falls back to a digest of the title, which changes when a late title arrives.
            details["item_id"] = player.item_identifier
            if player.metadata is not None:
                details["raw"] = raw_metadata(player.metadata)
        return details

    async def status(self):
        if self.api._connection is None or not self.api._connection.connected:
            raise ConnectionError("Companion connection closed")
        app = playing = None
        with suppress(exceptions.NotSupportedError):
            current = self.facade.metadata.app
            if current:
                app = {"bundle_id": current.identifier, "name": current.name}
        with suppress(exceptions.NotSupportedError):
            media = await self.facade.metadata.playing()
            playing = {
                "title": media.title,
                "artist": media.artist,
                "state": media.device_state.name.lower(),
                "content_identifier": media.content_identifier,
                "position_s": media.position,
                "duration_s": media.total_time,
            }
        airplay_state = getattr(self.stream_protocol, "playback_state", None)
        if airplay_state:
            playing = (playing or {}) | {"state": airplay_state}
        controls = {name: self.control_protocol(feature) for name, feature in PLAYBACK_FEATURES.items()}
        volume = None
        power = await self.power()
        if controls["set_volume"] == Protocol.Companion and power in ("on", "screensaver"):
            # Some receivers do not push volume changes, even after confirming a write.
            with suppress(exceptions.ProtocolError, TimeoutError):
                volume = await self.read_companion_volume()
        elif controls["set_volume"] == Protocol.MRP:
            level = self.facade.audio.get(Protocol.MRP).volume
            if level is not None and 0 <= level <= 100:
                volume = level
        available = {name: protocol is not None for name, protocol in controls.items()}
        if available["seek_absolute"] and playing and playing.get("position_s") is not None:
            available["skip_forward"] = available["skip_backward"] = True
        return {
            "playback_controls": available,
            "volume_percent": volume,
            "airplay_playing": airplay_state == "playing",
            "name": self.facade._config.name,
            "model": self.facade.device_info.model_str,
            "power": power,
            "app": app,
            "now_playing": playing,
            "volume_supported": self.facade.features.in_state(
                FeatureState.Available, FeatureName.VolumeUp, FeatureName.VolumeDown
            ),
            "keyboard_focused": self.facade.keyboard.text_focus_state == KeyboardFocusState.Focused,
            "tvos": self.facade.device_info.version or "unknown",
        }
