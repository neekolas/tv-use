# SPDX-License-Identifier: GPL-3.0-or-later
import asyncio
import base64
import io
import json
import logging
import os
import plistlib
import socket
import sys
import tempfile
from contextlib import suppress
from pathlib import Path
from unittest.mock import patch

from cryptography.exceptions import InvalidTag
from PIL import Image
from pymobiledevice3.bonjour import browse_remotepairing
from pymobiledevice3.dtx import _reader as dtx_reader
from pymobiledevice3.dtx.exceptions import DTXProtocolError
from pymobiledevice3.exceptions import PairingError, RemotePairingCompletedError
from pymobiledevice3.remote import tunnel_service
from pymobiledevice3.remote.siphash import validate_auth_tag
from pymobiledevice3.remote.userspace_tunnel import TunnelProvider, UserspaceRsdTunnel
from pymobiledevice3.services.accessibilityaudit import AccessibilityAudit
from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
from pymobiledevice3.services.dvt.instruments.screenshot import Screenshot
from zeroconf import (
    DNSIncoming,
    DNSOutgoing,
    DNSPointer,
    DNSQuestion,
    DNSService,
    DNSText,
)

from . import accessibility
from .buttons import ButtonWatch


class ScreenPairingInvalid(Exception):
    """The selected receiver cannot use the saved screen record."""


class PairingScreenUnavailable(ConnectionError):
    """The manual PIN service is not advertised until the pairing screen opens."""


class ScreenServiceMissing(ConnectionError):
    """Directed discovery received no RemotePairing endpoint."""


class ScreenshotFailure(Exception):
    """A fresh DVT capture failed before it could produce an image."""


async def capture_frame(dvt):
    # pymobiledevice3 11.61 reads a module-wide 30 MiB limit. Change the
    # budget only for the reader task that owns this short-lived DVT connection.
    # Other DTX connections in the same observer retain their original limit.
    reader_task = dvt.dtx._reader_task
    fragmenter = dtx_reader.DTXFragmenter
    assembly_error = None

    def assemble(fragment, buffered, limit):
        nonlocal assembly_error
        if asyncio.current_task() is not reader_task:
            return fragmenter(fragment, buffered, limit)
        if fragment.data_size > limit:
            logging.info("Receiving large DVT screenshot message (%d bytes)", fragment.data_size)
        try:
            return fragmenter(fragment, buffered, 64 * 1024**2)
        except DTXProtocolError as exc:
            # The DTX background reader closes the socket on failure, so
            # get_screenshot otherwise sees only a generic disconnect.
            assembly_error = exc
            raise

    with patch.object(dtx_reader, "DTXFragmenter", assemble):
        try:
            async with Screenshot(dvt) as service:
                return await service.get_screenshot()
        except Exception as exc:
            reason = str(assembly_error) if assembly_error else type(exc).__name__
            raise ScreenshotFailure(f"DVT capture failed ({reason}).") from exc


async def endpoint(host, manual):
    """Resolve only this TV's dynamic port with legacy unicast DNS-SD."""
    kind = "_remotepairing-manual-pairing" if manual else "_remotepairing"
    name = f"{kind}._tcp.local."
    loop = asyncio.get_running_loop()
    alias = port = None
    identifier = None
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setblocking(False)
        sock.connect((host, 5353))
        for _ in range(5):
            query = DNSOutgoing(0, multicast=False)
            query.add_question(DNSQuestion(name, 12, 1))
            if alias:
                for rrtype in (16, 33):
                    query.add_question(DNSQuestion(alias, rrtype, 1))
            await loop.sock_sendall(sock, query.packets()[0])
            try:
                records = DNSIncoming(
                    await asyncio.wait_for(loop.sock_recv(sock, 65535), 1)
                ).answers()
            except TimeoutError:
                continue
            for rr in records:
                if isinstance(rr, DNSPointer) and rr.name.lower() == name:
                    alias = rr.alias
            for rr in records:
                if not alias or rr.name.lower() != alias.lower():
                    continue
                if isinstance(rr, DNSService):
                    port = rr.port
                if isinstance(rr, DNSText):
                    data = rr.text
                    while data:
                        length = data[0]
                        item, data = data[1 : 1 + length], data[length + 1 :]
                        if item.startswith(b"identifier="):
                            identifier = item.split(b"=", 1)[1].decode()
            if port and (identifier or not manual):
                return identifier, port
    if not manual:
        raise ScreenServiceMissing("RemotePairing service not advertised at the selected address")
    raise PairingScreenUnavailable("RemotePairing service unavailable at configured address")


def emit(event):
    """Write one unsolicited event line. Responses and events share stdout, one JSON object per line."""
    print(json.dumps(event), flush=True)


class Observer:
    def __init__(self):
        self.host = os.environ["ATV_BRIDGE_HOST"]
        self.controller = os.environ["ATV_CONTROLLER_ID"]
        self.directory = Path(os.environ["ATV_BRIDGE_DATA"]) / "observer"
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.record = self.directory / "remote.plist"
        self.tunnel = UserspaceRsdTunnel(provider_factory=self.provider)
        self.audit = self.reader = None
        self.task = self.pin = None
        self.pairing = False
        # Held for each request. The button watch takes it before it closes a dead tunnel.
        self.busy = asyncio.Lock()
        self.opening = None
        self.buttons = ButtonWatch(self, emit)

    async def open_tunnel(self):
        """Open the tunnel once for every caller.

        pymobiledevice3 11.61 raises if a second aopen() starts while the first is running.
        Requests and the button watch therefore share one open task. A caller that
        arrives during a connect joins it and never waits for a connect of its own.
        """
        # A cancelled connect that is still unwinding is finished for new callers. Wait for it, then start again.
        while self.opening and not self.opening[0].done() and self.opening[0].cancelling():
            await asyncio.wait({self.opening[0]})
        # aopen() returns at once when the tunnel is already open.
        if self.opening is None or self.opening[0].done():
            self.opening = [asyncio.create_task(self.tunnel.aopen()), 0]  # The task and its waiters.
        opening = self.opening
        task = opening[0]
        opening[1] += 1
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled() and not asyncio.current_task().cancelling():
                raise ConnectionError("The tunnel open was cancelled") from None
            raise
        finally:
            opening[1] -= 1
            # Nobody wants this connect any more. Do not let it hold the tunnel.
            if not opening[1] and not task.done():
                task.cancel()

    async def cancel_open(self):
        if self.opening and not self.opening[0].done():
            self.opening[0].cancel()
            await asyncio.gather(self.opening[0], return_exceptions=True)

    def pairing_active(self):
        return self.pairing or bool(self.task and not self.task.done())

    async def tunnel_endpoint(self):
        # The TXT identifier rotates. pymobiledevice3 checks its authTag against the
        # receiver's altIRK. Older bridge records can still use directed DNS-SD.
        if os.environ.get("ATV_BRIDGE_BONJOUR") == "1" and self.record.exists():
            try:
                record = plistlib.loads(self.record.read_bytes())
            except plistlib.InvalidFileException as exc:
                raise ScreenPairingInvalid("Unreadable local pairing record") from exc
            if irk := record.get(tunnel_service.PEER_ALT_IRK_KEY):
                for answer in await browse_remotepairing(timeout=2):
                    identifier = answer.properties.get("identifier")
                    if identifier and validate_auth_tag(
                        irk, identifier, answer.properties.get("authTag", "")
                    ):
                        ips = [a.full_ip for a in answer.addresses]
                        if ips:
                            return (self.host if self.host in ips else ips[0]), answer.port
        if not self.host:
            raise ConnectionError("No matching RemotePairing Bonjour advertisement")
        _, port = await endpoint(self.host, False)
        return self.host, port

    async def provider(self):
        host, port = await self.tunnel_endpoint()
        record = self.record

        class Provider(tunnel_service.RemotePairingTunnelService):
            async def _validate_pairing(self):
                if not await super()._validate_pairing():
                    raise ScreenPairingInvalid("Receiver rejected the saved screen pairing")
                return True

            @property
            def pair_record_path(self):
                return record

        service = Provider(host, host, port)
        service.identifier = self.controller
        try:
            await service.connect(autopair=False)
        except (PairingError, InvalidTag, KeyError, ValueError, plistlib.InvalidFileException) as exc:
            raise ScreenPairingInvalid("Screen pairing authentication failed") from exc
        return TunnelProvider(service)

    async def pair_start(self):
        if self.task and not self.task.done():
            raise ValueError("A PIN prompt is already active")
        self.pairing = True
        try:
            return await self._pair_start()
        finally:
            self.pairing = False

    async def _pair_start(self):
        await self.close_channels()
        identifier, port = await endpoint(self.host, True)
        requested = asyncio.Event()
        self.pin = asyncio.get_running_loop().create_future()
        record = self.record
        pin = self.pin

        class Manual(tunnel_service.RemotePairingManualPairingService):
            @property
            def pair_record_path(self):
                return getattr(self, "staging_path", record)

            def save_pair_record(self):
                # Keep the previous verified record if the new write is interrupted.
                with tempfile.TemporaryDirectory(dir=record.parent) as directory:
                    self.staging_path = Path(directory) / "remote.plist"
                    try:
                        super().save_pair_record()
                        self.staging_path.replace(record)
                    finally:
                        del self.staging_path

            @property
            def pair_record(self):
                # An explicit manual pairing request always starts a new PIN
                # exchange. The tunnel provider separately loads the saved record.
                return None

            async def _request_pair_consent(self):
                # 11.61 exposes a terminal prompt here. Capture only its synchronous
                # input hook inside this single-purpose process. All wire exchanges
                # and proof checks remain in the upstream RemotePairing service.
                with patch.object(tunnel_service, "input", lambda _: "pending", create=True):
                    result = await super()._request_pair_consent()
                if result.pin != "pending":
                    raise ValueError("TV did not offer a PIN")
                requested.set()
                return result._replace(pin=await pin)

        service = Manual(identifier, self.host, port)
        service.identifier = self.controller

        async def pair():
            try:
                await service.connect()
            except RemotePairingCompletedError:
                pass
            finally:
                await service.close()

        self.task = asyncio.create_task(pair())
        prompt = asyncio.create_task(requested.wait())
        try:
            await asyncio.wait((self.task, prompt), return_when=asyncio.FIRST_COMPLETED)
            if self.task.done():
                await self.task
                return {"already_paired": True, "path": str(record)}
            return {}
        finally:
            prompt.cancel()
            with suppress(asyncio.CancelledError):
                await prompt

    async def close_channels(self):
        try:
            if self.reader:
                await self.reader.close()
                self.reader = None
            if self.audit:
                audit, self.audit = self.audit, None
                await audit.close()
        finally:
            # A close must not wait for a connect that another caller started.
            await self.cancel_open()
            await self.tunnel.aclose()

    async def _read_ax_once(self, method, maximum, menu_label):
        rsd = await self.open_tunnel()
        services = rsd.peer_info.get("Services", {})
        if self.audit is None and (
            method == "ax_running_apps" or accessibility.SERVICE not in services
        ):
            self.audit = AccessibilityAudit(rsd)
            await self.audit.__aenter__()
        if method == "ax_running_apps":
            return await accessibility.running_apps(self.audit)
        if self.reader is None:
            self.reader = accessibility.ScreenReader(self.audit, rsd)
            await self.reader.start()
            logging.info("Started passive screen reader")
        screen = await self.reader.observe(maximum, menu_label)
        if method == "ax_items":
            return screen
        return {"focused": screen["focused"], "app": screen["app"]}

    async def read_ax(self, req):
        if not self.record.is_file():
            raise FileNotFoundError("Remote Pairing is required")
        method = req["method"]
        maximum = req.get("max_items", 60)
        timeout = req.get("timeout", 25)
        if type(maximum) is not int or not 1 <= maximum <= 100:
            raise ValueError("max_items must be between 1 and 100")
        if type(timeout) not in (int, float) or not 0 < timeout <= 25:
            raise ValueError("timeout must be in (0, 25]")
        # The JSON-lines reader serializes requests. Setup, snapshot reads,
        # and a connection retry share the caller's time budget.
        try:
            async with asyncio.timeout(timeout):
                try:
                    return await self._read_ax_once(method, maximum, req.get("menu_label"))
                except TimeoutError:
                    raise
                except Exception:  # noqa: BLE001 - Rebuild the observation channels once.
                    logging.exception("AX request %s failed. Reopening the tunnel.", method)
                    await self.close_channels()
                    return await self._read_ax_once(method, maximum, req.get("menu_label"))
        except BaseException:
            await self.close_channels()
            raise

    async def call(self, req):
        method = req["method"]
        if method == "buttons_start":
            self.buttons.start()
            return {}
        if method == "buttons_stop":
            await self.buttons.stop()
            return {}
        if method == "ax_release":
            if self.reader:
                await self.reader.close()
                self.reader = None
            return {}
        if method == "health" and req.get("verify"):
            if not self.record.is_file():
                raise ScreenPairingInvalid("Saved screen pairing record is missing")
            async with asyncio.timeout(16):
                rsd = await self.open_tunnel()
            services = rsd.peer_info.get("Services", {})
            if DvtProvider.RSD_SERVICE_NAME not in services or not (
                accessibility.SERVICE in services or AccessibilityAudit.RSD_SERVICE_NAME in services
            ):
                raise ScreenPairingInvalid("Required screen services are no longer advertised")
        if method == "health":
            return {"paired": self.record.is_file(), "tunnel": self.tunnel.rsd is not None}
        if method == "pair_start":
            if req.get("replace"):
                await self.close_channels()
                self.record.unlink(missing_ok=True)
            return await self.pair_start()
        if method == "pair_finish":
            if not self.task or self.pin.done():
                raise ValueError("No active PIN prompt")
            self.pin.set_result(req["pin"])
            await self.task
            self.task = None
            return {"path": str(self.record)}
        if method in ("ax_items", "ax_focused", "ax_running_apps"):
            return await self.read_ax(req)
        if method == "screenshot":
            if not self.record.is_file():
                raise FileNotFoundError("Remote Pairing is required")
            width = int(req["max_width"])
            try:
                async with asyncio.timeout(25):
                    rsd = await self.open_tunnel()
                    async with DvtProvider(rsd) as dvt:
                        data = await capture_frame(dvt)
                with Image.open(io.BytesIO(data)) as img:
                    if img.width > width:
                        img = img.resize(
                            (width, max(1, round(img.height * width / img.width))),
                            Image.Resampling.LANCZOS,
                        )
                    out = io.BytesIO()
                    img.save(out, "PNG")
                return {"png": base64.b64encode(out.getvalue()).decode()}
            except BaseException:
                await self.close_channels()
                raise
        raise ValueError("Unknown observer method")

    async def close(self):
        await self.buttons.stop()
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self.close_channels()


async def main():
    logging.basicConfig(level=logging.INFO)
    os.umask(0o077)
    observer = Observer()
    try:
        while line := await asyncio.to_thread(sys.stdin.buffer.readline):
            try:
                async with observer.busy:
                    result = {"ok": True, **await observer.call(json.loads(line))}
            except Exception as exc:  # noqa: BLE001 - One JSON response per command, including driver failures.
                if isinstance(exc, PairingScreenUnavailable):
                    logging.debug("Waiting for the manual pairing service")
                elif isinstance(exc, FileNotFoundError):
                    logging.info("Observer requires Remote Pairing")
                else:
                    logging.exception("Observer request failed")
                if isinstance(exc, ScreenshotFailure):
                    code = "screen_unavailable"
                elif isinstance(exc, ScreenPairingInvalid):
                    code = "screen_pairing_invalid"
                elif isinstance(exc, PairingScreenUnavailable):
                    code = "pairing_screen_unavailable"
                elif isinstance(exc, ScreenServiceMissing):
                    code = "screen_service_missing"
                elif isinstance(exc, FileNotFoundError):
                    code = "pairing_required"
                else:
                    code = "observer_failed"
                result = {
                    "ok": False,
                    "code": code,
                    "message": str(exc) if isinstance(exc, ScreenshotFailure) else f"Observer request failed ({type(exc).__name__}).",
                }
            print(json.dumps(result), flush=True)
    finally:
        await observer.close()


if __name__ == "__main__":
    asyncio.run(main())
