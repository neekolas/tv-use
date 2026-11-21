import asyncio
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

import yaml

from ..live import TAILNET_DNS_FIX, probe, resolve_tailnet, tailnet_dns
from ..present.browsers import browser_status
from ..state import Fault, classify, now, reworded, success

LINKS = yaml.safe_load((Path(__file__).parents[1] / "data/deeplinks.yaml").read_text())
SYSTEM_APP_NAMES = {
    "com.apple.HeadBoard": "Home",
    "com.apple.TVSettings": "Settings",
    "com.apple.PineBoard": "System Shell",
    "com.apple.TVSystemUIService": "System UI",
    "com.apple.IdleScreen": "Screen Saver",
}
MEDIA_COMMANDS = ("play", "pause", "next", "previous")
KEYS = (
    "up",
    "down",
    "left",
    "right",
    "menu",
    "select",
    "home",
    "volume_up",
    "volume_down",
    "siri",
    "screensaver",
    "play_pause",
    "channel_increment",
    "channel_decrement",
    "guide",
    "page_up",
    "page_down",
    "play",
    "pause",
    "next",
    "previous",
)
TEXT = {"type": "string", "minLength": 1}
BOOL = {"type": "boolean"}
VIDEO_ID = {"type": "string", "pattern": "^[A-Za-z0-9_-]{11}$"}


def number(low, high):
    return {"type": "integer", "minimum": low, "maximum": high}


def schema(properties=None, required=(), **keywords):
    return {
        "type": "object",
        "properties": properties or {},
        "required": list(required),
        "additionalProperties": False,
        **keywords,
    }


def invalid(message):
    return Fault("invalid_arguments", message, "Use the tool's listed input schema.")


def youtube_id(url):
    """The 11-character video ID in a link or a bare ID. None when the text holds no valid ID."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    found = parse_qs(parts.query).get("v", [None])[0] or parts.path.rstrip("/").rsplit("/", 1)[-1]
    return found if re.fullmatch(VIDEO_ID["pattern"], found or "") else None


def link_info(url):
    parts = urlsplit(url)
    if not parts.scheme or parts.scheme.lower() in ("javascript", "file", "data"):
        raise invalid("Expected a web URL or app deep link.")
    for service, rule in LINKS.items():
        if parts.hostname in rule["hosts"] or parts.scheme.lower() in rule.get("schemes", []):
            if service == "youtube" and (identifier := youtube_id(url)):
                url = rule["forms"][0].format(id=identifier)
            if query := rule.get("query"):
                params = [
                    (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in query
                ]
                url = urlunsplit(parts._replace(query=urlencode(params + list(query.items()))))
            return url, rule
    return url, {}


def playback_identity(media):
    return tuple((media or {}).get(key) for key in ("state", "title", "artist", "content_identifier"))


class Tools:
    def __init__(self, engine, stage, sender, observer, state, verify_timeout=5):
        self.engine = engine
        self.stage = stage
        self.sender = sender
        self.observer = observer
        self.state = state
        self.verify_timeout = verify_timeout
        # Why the TV ended the last page, reported as screen.last_presentation_end. It changes only under the
        # stage lock: a dismissal sets it, and a new page or any other hide takes over and clears it.
        self.presentation_end = None
        self.taken_over = 0.0  # Monotonic time of the last takeover. An end from before it is stale.

    def take_over(self):
        """Call under the stage lock when a new page or a hide takes the screen."""
        self.presentation_end, self.taken_over = None, time.monotonic()

    async def tv_health(self):
        """Read bridge connectivity, access checks and protocol pairing status."""
        result = self.engine.health()
        result["browser"] = browser_status()
        resolver = self.engine.adapter.resolver
        result["tv"] = {"address": resolver.address, "address_source": resolver.source}
        result["discover_for_pairing"] = not (resolver.configured_host or resolver.configured_id)
        result["settings"] = {"airplay_home_only": self.engine.adapter.access_restricted}
        result["sender"] = getattr(self, "presenter", self.sender).status()
        hostname = self.stage.dns_status["hostname"]
        if hostname:
            await resolve_tailnet(self.stage, hostname)
        result["settings"]["magicdns"] = self.stage.dns_status
        result["tailnet_dns"] = await tailnet_dns()
        result["problems"] = (
            []
            if result["tailnet_dns"]["ok"]
            else [{"code": "tailnet_dns_missing", "fix": reworded(TAILNET_DNS_FIX)}]
        )
        invalid = getattr(self.observer, "invalid", False) is True
        if result["pairings"]["remote_pairing"] and not self.engine.pairing and (
            result["connected"] and self.engine.last_power in ("on", "screensaver")
        ):
            try:
                await self.observer.call("health", verify=True)
                invalid = False
            except Fault as exc:
                invalid = invalid or exc.code == "screen_pairing_invalid"
        if invalid and result["pairings"]["remote_pairing"]:
            result["problems"].append({
                "code": "screen_pairing_invalid",
                "fix": reworded(
                    "Run tv_pair with kind \"screen\" for this TV, or choose Configure → Re-pair screen in Home Assistant."
                ),
            })
            if "remote_pairing" not in result["pairing_needed"]:
                result["pairing_needed"].append("remote_pairing")
        if resolver.change:
            result["problems"].append(resolver.change)
        error = result["sender"].get("error", {})
        if (
            error.get("code") == "pairing_required"
            and self.state.pairings.get("airplay")
            and result["ok"]
        ):
            issue = Fault(
                "pairing_removed",
                "The sender was asked to pair again.",
                "Check AirPlay access and pair again.",
            )
            result.update(issue.result(state=issue.code))
        elif result["ok"] and error.get("code") in (
            "pairing_removed",
            "allow_access_changed",
            "unreachable",
            "tv_unreachable",
        ):
            result.update(ok=False, error=error, state=error["code"])
        result["classification"] = result["state"]
        return result

    async def foreground_app(self, status, focus=None, *, lookup_focus=False, observation=None):
        media_app = status.get("app")
        fallback = (media_app | {"source": "now_playing"}) if media_app else None
        if status["power"] not in ("on", "screensaver") or not self.state.receiver(
            "remote_pairing"
        ):
            return fallback
        if lookup_focus and observation is None:
            try:
                observation = await self.observer.call("ax_focused")
                focus = observation.get("focused")
            except Fault:
                return None
        owner = focus or {}
        observed = (observation or {}).get("app") or {}
        bundle = owner.get("bundle_id") or observed.get("bundle_id")
        if bundle:
            return {
                "bundle_id": bundle,
                "name": SYSTEM_APP_NAMES.get(bundle) or owner.get("app_name") or observed.get("name"),
                "source": "accessibility",
            }
        if observation is not None and "app" in observation:
            return None
        try:
            apps = [
                app
                for app in (await self.observer.call("ax_running_apps"))["apps"]
                if app.get("bundle_id")
            ]
        except Fault:
            apps = []
        marked = [app for app in apps if app.get("foreground")]
        owned = [a for a in apps if owner.get("pid") and a.get("pid") == owner["pid"]]
        candidates = owned or marked
        if len(candidates) != 1:
            return None
        match = candidates[0]
        return {
            "bundle_id": match["bundle_id"],
            "name": SYSTEM_APP_NAMES.get(match["bundle_id"], match.get("name")),
            "source": "accessibility",
        }

    async def status(self):
        status = await self.engine.status()
        return status | {"app": await self.foreground_app(status, lookup_focus=True)}

    async def tv_sleep(self):
        """Send the Sleep key to the Apple TV."""
        async with self.engine.session() as client:
            await client.hid("sleep", 0)
        return success("delivered", ["HID Sleep acknowledged"])

    async def tv_list_apps(self):
        """List installed apps with their names and bundle identifiers."""
        async with self.engine.session() as client:
            return success("listed", apps=await client.apps())

    async def _verify(self, client, expected, before, playback=False, identifier=None, timeout=None):
        deadline = asyncio.get_running_loop().time() + (
            self.verify_timeout if timeout is None else timeout
        )
        while True:
            after = await client.status()
            app = (
                after["app"] if playback else await self.foreground_app(after, lookup_focus=True)
            ) or {}
            media = after["now_playing"] or {}
            matches = expected and app.get("bundle_id") == expected
            if matches and not playback:
                return success("opened", [f"App reported {expected}"])
            changed = (after["app"] or {}).get("bundle_id") != (before["app"] or {}).get(
                "bundle_id"
            ) or playback_identity(media) != playback_identity(before["now_playing"])
            content = media.get("content_identifier", "")
            reported_id = youtube_id(content or "") if identifier else None
            target = identifier and reported_id == identifier
            changed_to_target = changed and (not reported_id or target)
            if matches and media.get("state") == "playing" and (target or changed_to_target):
                return success(
                    "playing",
                    [f"Now playing in {expected}", f"Title: {media.get('title') or 'unknown'}"],
                )
            if not expected and changed and media.get("state") == "playing" and not playback:
                return success("playing", ["Now-playing state changed after launch"])
            if asyncio.get_running_loop().time() >= deadline:
                return Fault(
                    "open_unverified",
                    "The requested app or playback could not be verified.",
                    "Check the screen or use a supported deep link.",
                ).result(evidence=["Launch acknowledged"])
            await asyncio.sleep(0.25)

    @staticmethod
    async def _app_id(client, requested, rule):
        if not requested and not rule:
            return None
        names = [requested] if requested else [rule["bundle_id"], *rule["names"]]
        apps = await client.apps()
        for name in names:
            for app in apps:
                if name.casefold() in (app["name"].casefold(), app["bundle_id"].casefold()):
                    return app["bundle_id"]
        if requested:
            raise Fault(
                "app_not_found",
                "The app is absent from the installed app list.",
                "Read tv://apps for installed apps.",
            )
        return rule["bundle_id"]

    @asynccontextmanager
    async def changing_app(self):
        if not self.state.receiver("remote_pairing"):
            yield
            return
        async with self.observer.app_switch():
            yield
            # The launch acknowledgement precedes the foreground transition.
            await asyncio.sleep(min(2, self.verify_timeout))

    async def launch(self, client, target):
        async with self.changing_app():
            await client.open(target)

    async def tv_open(self, app=None, url=None, verify=True):
        """Launch an installed app or deep link, then check the reported app and playback."""
        url, rule = link_info(url) if url else (None, {})
        async with self.engine.session() as client:
            expected = await self._app_id(client, app, rule)
            before = await client.status() if verify else None
            await self.launch(client, url or expected)
            result = (
                await self._verify(client, expected, before)
                if verify
                else success("opened", ["Launch acknowledged; verification disabled"])
            )
            if note := rule.get("reliability"):
                result["evidence"].append(note)
            return result

    async def tv_play_youtube(self, video_id=None, url=None, timeout=None, after_launch=None):
        """Play a YouTube video ID, trying both supported URL forms until playback is observed."""
        rule = LINKS["youtube"]
        supplied = video_id or url
        if re.fullmatch(VIDEO_ID["pattern"], supplied):
            video_id = supplied
        else:
            parts = urlsplit(supplied)
            if parts.hostname not in rule["hosts"] and parts.scheme != "youtube":
                raise invalid("Expected a YouTube URL or video ID.")
            video_id = youtube_id(supplied)
            if not video_id:
                raise invalid("Expected a video ID in the YouTube URL.")
        attempts = []
        for form in rule["forms"]:
            try:
                async with self.engine.session() as client:
                    expected = await self._app_id(client, None, rule)
                    before = await client.status()
                    await self.launch(client, form.format(id=video_id))
                if after_launch and await after_launch():
                    async with self.engine.session() as client:
                        await self.launch(client, form.format(id=video_id))
                    await after_launch()
                async with self.engine.session() as client:
                    result = await self._verify(
                        client, expected, before, playback=True, identifier=video_id, timeout=timeout
                    )
                attempts.extend(result["evidence"])
                if result["ok"]:
                    return result | {"evidence": attempts}
            except Fault as exc:
                attempts.append(str(exc))
                if exc.code not in ("command_rejected", "unreachable"):
                    break
        return Fault(
            "playback_unverified",
            "Could not confirm YouTube playback.",
            "Check that YouTube is installed and the video is available.",
        ).result(evidence=attempts)

    async def tv_press(self, keys, hold_ms=0, settle_ms=600, screenshot=False, return_focus=False, *, switching_app=False, gap_ms=0):
        """Press an ordered key sequence once. An uncertain delivery must never be replayed automatically."""
        if hold_ms and any(key in MEDIA_COMMANDS for key in keys):
            raise invalid(
                "hold_ms requires HID keys. Play, pause, next and previous are media commands."
            )
        count = 0
        attempted = False
        try:
            async with self.engine.session() as client:
                for index, key in enumerate(keys):
                    if index and gap_ms:
                        await asyncio.sleep(gap_ms / 1000)
                    if key == "home" or switching_app:
                        async with self.changing_app():
                            attempted = True
                            await client.hid(key, hold_ms)
                    else:
                        attempted = True
                        await client.hid(key, hold_ms)
                    count += 1
                if settle_ms:
                    await asyncio.sleep(settle_ms / 1000)
                result = success(
                    "delivered",
                    [f"{count} keys acknowledged"],
                    delivered="confirmed",
                    confirmed_keys=count,
                )
                if return_focus:
                    try:
                        result["focused"] = (await self.observer.call("ax_focused"))["focused"]
                    except Exception as exc:  # noqa: BLE001 - Observation cannot invalidate acknowledged delivery.
                        result.update(focused=None, focus_error=classify(exc).result()["error"])
        except Exception:
            if not attempted:
                raise
            return Fault(
                "delivery_uncertain",
                "The key sequence lost confirmation.",
                "Inspect the screen before issuing another key.",
                delivered="uncertain",
                confirmed_keys=count,
            ).result(evidence=[f"{count} of {len(keys)} keys acknowledged"])
        if screenshot:
            try:
                result["screenshot"] = await self.tv_screenshot()
            except Exception as exc:  # noqa: BLE001 - A failed screenshot does not invalidate confirmed key delivery.
                result["screenshot"] = classify(exc).result()
        return result

    async def tv_type(self, text, clear=True):
        """Set or append text in a focused keyboard field and read it back for verification."""
        async with self.engine.session() as client:
            if not await client.type(text, clear):
                raise Fault(
                    "text_unverified",
                    "Keyboard readback differed.",
                    "Check keyboard focus and the text field.",
                )
        return success("typed", ["Keyboard text matched readback"])

    async def tv_screenshot(self, max_width=1280):
        """Capture a scaled PNG for visual checks. tvOS shows a Screenshot Taken toast on each capture."""
        capture = await self.observer.call("screenshot", max_width=max_width)
        return success(
            "captured",
            ["DVT screenshot"],
            captured_at=now(),
            drm_black_possible=True,
            toast_visible=True,
            image={"mimeType": "image/png", "data": capture["png"]},
        )

    async def present(self, *, url, html, files, bundle, title, audio, remote="off"):
        async with self.stage.lock:
            if files is not None:
                bundle = await self.stage.store.put_files(files)
            elif html is not None:
                bundle = await self.stage.store.put(html=html)
            if bundle:
                try:
                    self.stage.store.file(bundle, "index.html")
                except Exception as exc:
                    raise Fault(
                        "page_unreachable", "The bundle is unavailable.", "Upload the bundle again."
                    ) from exc
            page_id = bundle or uuid.uuid4().hex
            target = url or f"/c/{bundle}/index.html"
            embedded = await probe(self.stage, url) if url else True
            sender = self.live_mirror if url else self.sender
            previous = getattr(self, "presenter", None)
            if previous and previous is not sender:
                if getattr(previous, "active", None) is sender:
                    await previous.release_supervision()
                elif getattr(sender, "active", None) is not previous:
                    await previous.stop()
            self.presenter = sender
            self.live_mirror.audio = audio
            # The new page takes over here. A failure below hides it, and the old end record stays cleared.
            self.take_over()
            await self.stage.show(
                page_id, target, title, url=url, audio=audio,
                **({"remote": remote} if remote != "off" else {}),
            )
            try:
                direct = False
                try:
                    # Even pages that advertise a framing restriction first go
                    # through the stage. Switch the actual browser on refusal.
                    await sender.start(self.stage.url)
                    if not embedded:
                        raise TimeoutError
                except TimeoutError:
                    if not url:
                        return {
                            "page_id": page_id,
                            "mode": "mirror",
                            "verified": False,
                            "evidence": ["The mirror started but the stage has not confirmed loading."],
                        }
                    await sender.start(url)
                    direct = True
                    self.stage.direct_browser = sender
                active = getattr(sender, "active", sender)
                if active.mode == "mirror":
                    verified = await active.confirm_frame()
                    if url:
                        await active.verify_document(url, page_id, direct, self.stage.page["revision"])
                    evidence = [
                        "Browser loaded the page at top level" if direct else "Stage loaded the iframe"
                    ]
                    if verified:
                        evidence.append("tvpipe reported frames pushed to the receiver")
                else:
                    # The first video requires recording before the TV can fetch it.
                    try:
                        async with asyncio.timeout(8):
                            while active.status()["state"] not in ("showing", "failed"):
                                await asyncio.sleep(0.1)
                    except TimeoutError:
                        pass
                    status = active.status()
                    if not status["ok"]:
                        error = status["error"]
                        raise Fault(error["code"], error["message"], error["remediation"])
                    verified = bool((await self.engine.status()).get("airplay_playing"))
                    evidence = status["evidence"]
                return {
                    "page_id": page_id,
                    "mode": active.mode,
                    "verified": verified,
                    "evidence": evidence,
                }
            except BaseException as exc:
                await sender.stop()
                await self.stage.hide()
                if isinstance(exc, Fault):
                    sender.fail(exc)
                raise

    async def tv_hide(self, still=None, end=None):
        """Stop presenting and close the stage browser. With `still`, only if it returns True under the stage lock.
        A dismissal passes `end`, the record of why the TV ended the page. Any other hide takes over and clears it."""
        async with self.stage.lock:
            if still and not still():
                return None
            if not end:
                self.take_over()
            await getattr(self, "presenter", self.sender).stop()
            await self.stage.hide()
            if end:
                # A dismissal records the end once the page is gone.
                self.presentation_end = end
        return success("hidden", ["Sender stopped"])
