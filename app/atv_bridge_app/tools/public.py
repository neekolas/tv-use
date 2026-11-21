"""The v1 operation dispatcher shared by HTTP, MCP and Home Assistant."""

import asyncio
import logging
import math
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

import yaml
from jsonschema import Draft202012Validator

from ..live import TAILNET_DNS_FIX
from ..present.interaction import PageInteraction
from ..present.media import MediaPlayback
from ..present.remote_input import EXIT_WINDOW_S, EXITS, RemoteInput
from ..state import Fault, classify, now, reworded
from . import link_info, playback_identity
from .contract import NO_SCREEN, RUN_HELP, SCHEMAS, UNKNOWN, canonical, checked, outcome, public_error, schema
from ..history import report
from .sequence import run_sequence
from .deep_link import confirm_custom_link, link_targets, observed, title_action
from .pair import exchange
from .prompts import prompt_choice
from .navigation import label, match_app, matches_label, navigate, normal

KEY_GAP_MS = 100
# Longer than any single tool's own deadline (tv_sequence allows 90 s), so it only catches real hangs.
TOOL_TIMEOUT = 150
_LOG = logging.getLogger(__name__)
RECIPES = yaml.safe_load((Path(__file__).parents[1] / "data/apps.yaml").read_text())


class PublicTools:
    def __init__(self, ops, mirror_size, page_scripts=True):
        self.page_scripts = page_scripts
        self.ops, self.engine, self.stage = ops, ops.engine, ops.stage
        self.size = mirror_size
        self.lock = asyncio.Lock()
        self.wait_timeout = 10
        self.play_timeout = 20
        self.settle_ms = 300
        self.tool_timeout = TOOL_TIMEOUT
        self.last_screen = UNKNOWN.copy()
        self.last_look = None
        self.profile_name = None
        self.action_evidence = []
        self.prompt_view = None
        self.media = MediaPlayback(self.engine)
        self.page_interaction = PageInteraction(ops)
        self.remote_input = RemoteInput(ops, self.page_interaction)
        # Until this monotonic time, an end of the page follows a Menu or Home that a tool sent.
        self.tool_exit_until = 0.0
        self.dismissals = set()
        if mirror := getattr(ops, "live_mirror", None):
            mirror.exit_press = self.remote_input.exit_press
            mirror.events.subscribe(lambda status: self.mirror_status(mirror, status))

    def mirror_status(self, mirror, status):
        """Dismiss the page when the TV ends its mirror, as the person did with Menu or Home."""
        if status.get("state") != "ended" or not self.stage.page:
            return
        # Decide the reason now. The dismissal can wait seconds for the stage lock.
        reason = "ended_by_tool" if time.monotonic() <= self.tool_exit_until else "ended_on_tv"
        end = {"reason": reason, "at": now(), "page_id": self.stage.page["page_id"]}
        task = asyncio.create_task(
            self.dismiss_ended(mirror, mirror.runner, end, time.monotonic(), status["evidence"])
        )
        self.dismissals.add(task)
        task.add_done_callback(self.dismissals.discard)

    async def dismiss_ended(self, mirror, run, end, ended_at, evidence):
        def current():
            # A page or hide that took over after the end, a newer mirror run, or another presenter keeps the screen.
            presenter = getattr(self.ops, "presenter", self.ops.sender)
            return (
                self.ops.taken_over < ended_at
                and mirror.runner is run
                and getattr(presenter, "active", presenter) is mirror
            )

        try:
            if not await self.ops.tv_hide(still=current, end=end):
                return
        except Exception:  # noqa: BLE001 - A background task has no caller to report to.
            _LOG.exception("Could not dismiss page %s after the TV ended its mirror", end["page_id"])
            return
        # tv_page_run and failed calls report the last screen without reading it again.
        self.last_screen["presenting"] = None
        self.last_screen["last_presentation_end"] = end
        _LOG.info("The TV ended the mirror of page %s (%s, %s). The page is dismissed and is not shown again.",
                  end["page_id"], "; ".join(evidence), end["reason"])

    @contextmanager
    def tool_exit(self, keys):
        """Mark a Menu or Home that a tool sends while a page shows, so that the end it causes reads ended_by_tool."""
        marked = bool(self.stage.page) and any(key in EXITS for key in keys)
        if marked:
            # The TV can end the mirror before the send returns.
            self.tool_exit_until = math.inf
        try:
            yield
        finally:
            if marked:
                self.tool_exit_until = time.monotonic() + EXIT_WINDOW_S

    async def snapshot(self, focus=None, read_focus=True, observation=None, *, status=None):
        if status is None:
            status = await self.engine.status()
        if read_focus and status["power"] not in ("off", "unknown"):
            try:
                observation = await self.ops.observer.call("ax_focused")
                focus = observation["focused"]
            except Fault:
                focus, observation = None, {"app": None}
        if not read_focus and observation is None:
            observation = {"app": None}
        app = await self.ops.foreground_app(status, focus, observation=observation)
        media = status.get("now_playing")
        page = self.stage.page
        self.last_screen = {
            "power": status["power"],
            "keyboard": status["keyboard_focused"],
            "focus": {"label": label(focus), "role": focus.get("role"), "value": focus.get("value")} if focus else None,
            "app": {"id": app["bundle_id"], "name": app["name"], "source": app["source"]}
            if app
            else None,
            "playing": {k: media.get(k) for k in ("title", "state", "position_s", "duration_s")}
            if media
            else None,
            "presenting": {
                "page_id": page["page_id"],
                "title": page.get("title"),
                "url": page.get("url"),
            }
            if page
            else None,
        }
        if page and (remote := self.remote_input.status()):
            self.last_screen["presenting"]["remote"] = remote
        if not page and self.ops.presentation_end:
            self.last_screen["last_presentation_end"] = self.ops.presentation_end
        return self.last_screen.copy()

    def failure(self, name, fault):
        error = public_error(fault)
        result = outcome(
            error["message"],
            fault.fields.get("evidence", []),
            {k: v for k, v in fault.fields.items() if k != "evidence"},
            False,
        )
        result.update(ok=False, error=error)
        if name not in NO_SCREEN:
            result["screen"] = self.last_screen.copy()
        return result

    def audit_failure(self, name, args, fault):
        result = self.failure(name, fault)
        self.ops.state.audit(name, args, result)
        return result

    async def call(self, name, args, *, replace=False):
        started = time.monotonic()
        _LOG.info("Tool %s received", name)
        result = await self.dispatch(name, args, replace)
        _LOG.info(
            "Tool %s finished: %s in %d ms",
            name,
            "ok" if result["ok"] else result["error"]["code"],
            (time.monotonic() - started) * 1000,
        )
        return result

    async def dispatch(self, name, args, replace):
        args = canonical(name, args)
        spec = SCHEMAS.get(name, schema() if name == "tv_hide" else None)
        if spec is None or not Draft202012Validator(spec).is_valid(args):
            return self.audit_failure(
                name,
                args,
                Fault(
                    "invalid_arguments",
                    "The request does not match the tool schema.",
                    "Use tools/list for accepted arguments.",
                ),
            )
        if name == "tv_history":
            # History reads stored entries. It must not wait for the TV lock or read the screen.
            try:
                result = await self.tv_history(**args)
            except Fault as fault:
                result = self.failure(name, fault)
            self.ops.state.audit(name, args, result)
            return result
        waiting = time.monotonic()
        try:
            await asyncio.wait_for(self.lock.acquire(), self.wait_timeout)
        except TimeoutError:
            _LOG.warning("Tool %s gave up waiting for the TV lock", name)
            return self.audit_failure(
                name,
                args,
                Fault(
                    "busy", "Another call is using the TV.", "Retry after the current call completes."
                ),
            )
        if (waited := time.monotonic() - waiting) > 1:
            _LOG.info("Tool %s waited %d ms for the TV lock", name, waited * 1000)
        deadline = asyncio.timeout(self.tool_timeout)
        try:
            async with deadline:
                self.action_evidence = []
                self.prompt_view = None
                if (
                    name == "tv_show"
                    and args
                    and hasattr(self, "admit")
                    and (args.get("url") or self.ops.sender.mode != "play_url")
                ):
                    await self.admit(replace)
                already_off = name == "tv_power" and not args["on"] and (await self.engine.status())["power"] == "off"
                if name not in ("tv_look", "tv_status", "tv_pair", "tv_sequence", "tv_page_run") and not already_off:
                    await self.prepare_action(force=name == "tv_power" and args["on"])
                result = await getattr(self, name)(**args)
                if self.action_evidence:
                    result["evidence"][:0] = self.action_evidence
                if name != "tv_status" and "screen" not in result:
                    result["screen"] = self.last_screen.copy() if name == "tv_page_run" else await self.snapshot()
                if name == "tv_look":
                    self.last_look = {k: v for k, v in result.items() if k != "image"}
        except Exception as exc:  # noqa: BLE001 - Both public transports use this contract.
            if deadline.expired():
                _LOG.warning("Tool %s did not finish within %d s and was cancelled", name, self.tool_timeout)
                fault = Fault(
                    "uncertain",
                    f"The call did not finish within {self.tool_timeout} s and was stopped. An action may have arrived.",
                    "Read tv_status or tv_look before retrying.",
                )
            else:
                fault = classify(exc)
            if name not in ("tv_status", "tv_pair", "tv_page_run"):
                try:
                    async with asyncio.timeout(10):
                        await self.snapshot(
                            read_focus=fault.code
                            not in ("screen_unavailable", "observer_failed", "observer_unavailable")
                        )
                except TimeoutError:
                    pass
            result = self.failure(name, fault)
            if self.action_evidence:
                result["evidence"][:0] = self.action_evidence
            if name == "tv_pair":
                result["screen"] = UNKNOWN.copy()
        finally:
            try:
                if hasattr(self, "reconcile"):
                    self.reconcile()
            finally:
                self.lock.release()
        self.ops.state.audit(name, args, result)
        return result

    async def apps(self):
        try:
            await asyncio.wait_for(self.lock.acquire(), self.wait_timeout)
        except TimeoutError as exc:
            raise Fault(
                "busy", "Another call is using the TV.", "Retry after the current call completes.", True
            ) from exc
        try:
            return [
                {"id": a["bundle_id"], "name": a["name"]}
                for a in (await self.ops.tv_list_apps())["apps"]
            ]
        finally:
            self.lock.release()

    async def takeover(self):
        await self.media.stop()
        if self.stage.page:
            await self.ops.tv_hide()
        else:
            # Nothing to hide, but the call still takes over, so an end record no longer applies.
            async with self.stage.lock:
                self.ops.take_over()

    async def tv_power(self, on):
        """Wake or sleep the receiver explicitly. Ends its bridge presentation, requests power and checks the reported state.
        Verified means screen.power reached on/screensaver or off. An unconfirmed transition returns uncertain.
        For example, tv_power {"on":false}."""
        await self.takeover()
        if not on:
            await self.engine.sleep_verified()
        deadline = asyncio.get_running_loop().time() + 5
        while True:
            screen = await self.snapshot(read_focus=on)
            if screen["power"] in (("on", "screensaver") if on else ("off",)):
                return outcome(
                    f"The TV is {'on' if on else 'off'}.", ["Power state reported by the TV"]
                ) | {"screen": screen}
            if asyncio.get_running_loop().time() >= deadline:
                raise Fault(
                    "uncertain",
                    "The TV did not confirm its power state.",
                    "Check tv_look before retrying.",
                )
            await asyncio.sleep(0.25)

    async def prepare_action(self, force=False):
        checked(await self.engine.ready(force=force))
        await self.prepare_ui()

    async def prepare_ui(self):
        if self.engine.last_power == "screensaver":
            await self.press("menu")
            for _ in range(8):
                if (await self.engine.status())["power"] == "on":
                    break
                await asyncio.sleep(0.25)
            else:
                raise Fault("uncertain", "The screen saver is still active.", "Read tv_look before acting.")
            self.action_evidence.append("Exited screen saver")
        await self.resolve_prompts(system=True)

    async def resolve_prompts(self, system=False):
        if not self.ops.state.receiver("remote_pairing"):
            return False
        changed = False
        previous = None
        for _ in range(4):
            try:
                view = await self.read_observation()
            except Fault:
                return changed
            preferred = self.default_profile if system else self.app_profile or self.profile_name or self.default_profile
            choice = prompt_choice(view, system=system, preferred=preferred)
            if not choice:
                # Nothing has been pressed since this read, so the action can reuse it as its starting screen.
                self.prompt_view = view
                return changed
            if choice == previous:
                raise Fault("uncertain", "The same prompt is still visible.", "Read tv_look before acting.")
            description, target = choice
            await navigate(self.observe, self.press, target)
            focus, _ = await self.observe()
            if normal(label(focus)) != normal(target):
                raise Fault("uncertain", "The intended prompt choice is not focused.", "Read tv_look.")
            await self.press("select")
            self.action_evidence.append(f"{description}: {target}")
            if system:
                self.profile_name = target
            previous, changed = choice, True
            await asyncio.sleep(0.35)
        raise Fault("uncertain", "Launch prompts did not settle.", "Read tv_look.")

    async def settings_root(self):
        previous = None
        for _ in range(10):
            view = await self.read_observation()
            items = [normal(label(i)) for i in view["items"]]
            if (view.get("app") or {}).get("bundle_id") != "com.apple.TVSettings" or not items:
                break
            # The devices submenu also exposes both "Settings" and "Remotes and Devices".
            # Require a sibling root row before starting a path from this screen.
            if "remotes and devices" in items and {"network", "video and audio", "general"}.intersection(items):
                return
            if previous == items:
                break
            previous = items
            await self.press("menu")
            await asyncio.sleep(0.35)
        raise Fault("uncertain", "Could not verify the Settings root menu.", "Open Settings on the TV and retry the path.")

    async def tv_pair(self, kind, **args):
        """Start or repair access for a registered receiver after checking tv_status. Starts a PIN exchange and can open Settings for screen pairing.
        Relay data.ask_user, poll with pairing_id and submit the code as text. Pending exchanges return verified=false and an unknown screen.
        Completion verifies a Companion app list or accessibility read. For example, tv_pair {"kind":"screen"}."""
        return await exchange(self, kind, **args)

    async def tv_status(self):
        """Check health before setup or troubleshooting. Reads connection state, pairings and capabilities without waking the TV.
        A verified check can still list problems in data.problems. With no default, inspect each row of data.tvs.
        For example, tv_status {}."""
        s = await self.engine.status()
        h = await self.ops.tv_health()
        p = h["pairings"]
        problems = []
        if not h["ok"]:
            e = h["error"]
            problems.append(public_error(Fault(e["code"], e["message"], e["remediation"])))
        can = s["connected"] and not problems
        problems.extend(h["problems"])
        screen_invalid = any(issue["code"] == "screen_pairing_invalid" for issue in problems)
        screen_ready = p["remote_pairing"] and not screen_invalid
        control_invalid = any(issue["code"] == "pairing_removed" for issue in problems)
        for issue in problems:
            if issue["code"] in ("not_paired", "pairing_removed"):
                issue["fix"] = 'Run tv_pair with kind "control" for this TV.'
        if not p["remote_pairing"]:
            problems.append(
                {
                    "code": "screen_unavailable",
                    "fix": 'Run tv_pair with kind "screen" for this TV.',
                }
            )
        if self.media.problem:
            problems.append(public_error(self.media.problem))
        if h["settings"]["magicdns"]["resolved"] is False:
            problems.append(
                {
                    "code": "page_unreachable",
                    "fix": reworded(TAILNET_DNS_FIX),
                }
            )
        return outcome(
            "The bridge checked the TV and its pairings.",
            h["evidence"],
            {
                "tv": {
                    **h["tv"],
                    "name": s.get("name"),
                    "model": s.get("model"),
                    "tvos": s.get("tvos"),
                    "reachable": s["connected"],
                },
                "tailnet_dns": h["tailnet_dns"],
                "browser": h["browser"],
                "pairings": {
                    "control": "invalid" if control_invalid else "ok" if p["companion"] else "missing",
                    "screen": "invalid" if screen_invalid else "ok" if screen_ready else "missing",
                },
                "capabilities": {
                    "show": bool(can and p["airplay"]),
                    "look_image": s["connected"] and screen_ready,
                    "look_text": s["connected"] and screen_ready,
                    "play_media": bool(
                        can
                        and p["airplay"]
                        and getattr(self.engine.adapter, "supports_play_url", False)
                    ),
                },
                "problems": problems,
            },
        )

    async def tv_history(self, **filters):
        """Read what was watched on the TVs, to learn tastes or find a title again. Read-only: it never wakes or changes a TV.
        data.entries lists items newest first with app, title, series, artist, start, end and watched_s, the seconds actually played.
        data.summary totals the whole match with top apps, series and artists. data.now lists what is playing. Omit tv for all TVs.
        The history is private. It is stored only on the Home Assistant host and served only to HA administrators. For example, tv_history {"since":"7d","app":"YouTube"}."""
        return await report([({}, self.history)], **filters)

    async def read_observation(self, *, single_read=False):
        try:
            observation = await self.ops.observer.call("ax_items", max_items=100)
            if single_read:
                return {"focused": None, "app": None} | observation
            focus = (
                observation["focused"]
                if "focused" in observation
                else (await self.ops.observer.call("ax_focused"))["focused"]
            )
            return observation | {"focused": focus}
        except Fault as exc:
            raise Fault(
                "screen_unavailable",
                "Accessibility text is unavailable.",
                "Check Remote Pairing in Home Assistant.",
            ) from exc

    async def observe(self):
        observation = await self.read_observation()
        return observation["focused"], observation["items"]

    async def tv_look(self, image=False, max_width=1280):
        """Inspect the UI before acting or after uncertain input. Reads accessibility without waking the TV. Use tv_status for connection health.
        A verified read returns labels in data.items and focus in screen, or a requested PNG capture. Images take an extra capture and show a tvOS toast.
        With no default, returns text summaries in data.tvs. For example, tv_look {"image":true}."""
        observation = {"app": None}
        try:
            observation = await self.read_observation()
            focus, items = observation["focused"], observation["items"]
        except Fault:
            if not image:
                raise
            focus, items = None, []
        if not image and not focus and not items:
            raise Fault(
                "screen_unavailable",
                "The accessibility service returned no labels or focus.",
                "Wait for the app to settle and read tv_look again.",
                items=[],
            )
        if focus and not focus.get("role"):
            match = next((item for item in items if label(item) == label(focus)), {})
            focus = {**match, **focus}
        screen = await self.snapshot(focus, False, observation)
        data = {
            "items": [
                {
                    "label": label(i),
                    "role": i.get("role"),
                    "value": i.get("value"),
                    "focused": bool(focus)
                    and (
                        (i["uid"] == focus["uid"])
                        if i.get("uid") and focus.get("uid")
                        else normal(label(i)) == normal(label(focus))
                    ),
                }
                for i in items
            ],
        }
        result = outcome(
            "Read the TV screen.", ["Accessibility observation"] if focus or items else [], data
        ) | {"screen": screen}
        if image:
            try:
                capture = checked(await self.ops.tv_screenshot(max_width))
            except Fault as exc:
                raise Fault(
                    "screen_unavailable", f"Screen capture failed. {exc}", "Read tv_look again for a fresh capture."
                ) from exc
            result["image"] = capture["image"]
            data.update(toast_visible=True, captured_at=capture["captured_at"])
            result["evidence"].append("DVT screenshot")
        return result

    async def press(self, key, hold=0, *, switching_app=False):
        with self.tool_exit([key]):
            return checked(await self.ops.tv_press([key], hold_ms=hold, settle_ms=self.settle_ms, switching_app=switching_app))

    @staticmethod
    def changed(before, after):
        return (
            any(before[k] != after[k] for k in ("power", "focus", "keyboard"))
            or (before["app"] or {}).get("id") != (after["app"] or {}).get("id")
            or playback_identity(before["playing"]) != playback_identity(after["playing"])
        )

    async def settled(self, before, key):
        after = await self.snapshot()
        if key in ("home", "menu", "select") and (not after["focus"] or not self.changed(before, after)):
            await asyncio.sleep(0.3)
            after = await self.snapshot()
        return after

    async def send_keys(self, keys, hold, path):
        with self.tool_exit(keys):
            result = await self.ops.tv_press(keys, hold_ms=hold, settle_ms=self.settle_ms, gap_ms=KEY_GAP_MS)
        if not result["ok"]:
            path += keys[: result.get("confirmed_keys", 0)]
        try:
            checked(result)
        except Fault as exc:
            exc.fields.update(confirmed_keys=len(path), keys=path)
            raise
        path += keys

    async def tv_remote(self, keys=None, text=None, submit=False, hold_ms=0, swipe=None, check_each=False):
        """Send keys, a swipe or text when a labeled target is unavailable. Can wake the TV. Keys go out in one burst and the screen is read once at the end, plus after Home. check_each reads the screen after every key and stops if focus is unreadable. Text checks keyboard readback.
        data.confirmed_keys counts acknowledgments. verified=false means no screen change was seen. On lost delivery inspect screen before sending more.
        Use tv_sequence for checkpointed runs. For example, tv_remote {"keys":["menu"]}."""
        path = []
        view, self.prompt_view = self.prompt_view, None
        screen = await (self.snapshot(view["focused"], False, view) if view else self.snapshot())
        verified = True
        evidence = []
        if text is not None:
            checked(await self.ops.tv_type(text))
            evidence.append("Keyboard text matched readback")
            screen = await self.snapshot()
            keys = ["select"] if submit else []
        if swipe:
            try:
                async with self.engine.session() as client:
                    await client.swipe(swipe)
            except Exception as exc:
                raise Fault(
                    "uncertain", "Swipe delivery could not be confirmed.", "Read tv_look before acting."
                ) from exc
            after = await self.snapshot()
            verified = self.changed(screen, after)
            screen = after
        keys = list(keys or [])
        # Per-key mode checks every key. Burst mode still re-reads after Home, before more keys land in a new app.
        cuts = [i + 1 for i, key in enumerate(keys) if check_each or key == "home"]
        chunks = [keys[a:b] for a, b in zip([0] + cuts, cuts + [len(keys)]) if a < b]
        start = screen
        for index, chunk in enumerate(chunks):
            await self.send_keys(chunk, hold_ms, path)
            after = await self.settled(screen, chunk[-1])
            if check_each:
                verified &= self.changed(screen, after)
            screen = after
            if index < len(chunks) - 1 and screen["focus"] is None:
                raise Fault(
                    "uncertain",
                    "Focus could not be read between remote presses.",
                    "Read tv_look before sending the rest.",
                    confirmed_keys=len(path),
                    keys=path,
                )
        if keys and not check_each:
            verified &= self.changed(start, screen)
        evidence.append(
            "Screen change observed" if verified else "Delivery acknowledged; screen change unconfirmed"
        )
        result = outcome(
            "The TV acknowledged the remote input.",
            evidence,
            {"keys": path, "delivered": "confirmed", "confirmed_keys": len(path)},
            verified,
        )
        return result | ({"screen": screen} if screen else {})

    async def tv_sequence(self, steps, for_each=None):
        """Run repeated remote work in one call on one TV. Can wake the receiver. Holds its tool lock across up to 50 expanded steps and a 90-second execution deadline.
        Keys skip per-key screen reads. Use expect to check the next action's preconditions. On failure inspect data.steps, data.stopped_at and the final screen before continuing.
        An acknowledged raw key leaves verified=false even when a later checkpoint passes. For example, tv_sequence {"steps":[{"key":"menu"},{"expect":{"app":"Settings"}}]}."""
        return await run_sequence(self, steps, for_each)

    async def tv_navigate(self, to, select=False, within=None, *, exact=False):
        """Focus one accessible label, optionally selecting it. Can wake the TV. Reads focus between direction keys, with a 40-press limit.
        Inspect data.path and screen.focus. Selection is verified when focus moves or the row value changes. Use tv_sequence for several guarded actions.
        For example, tv_navigate {"to":"General"}."""
        previous_value = None
        async def observe_target():
            nonlocal previous_value
            focus, items = await self.observe()
            if matches_label(focus, to, exact):
                previous_value = focus.get("value")
            return focus, items
        path = await navigate(observe_target, self.press, to, select, within, exact=exact)
        screen = await self.snapshot()
        # The first snapshot during a menu transition can still describe the
        # old page or have no focused row. Only repeat the observation.
        if select:
            for _ in range(2):
                if screen["focus"] and (not matches_label(screen["focus"], to, exact) or (previous_value is not None and screen["focus"].get("value") not in (None, previous_value))):
                    break
                await asyncio.sleep(0.3)
                screen = await self.snapshot()
        moved = not matches_label(screen["focus"], to, exact)
        value_changed = (not moved and previous_value is not None and screen["focus"] and
                         screen["focus"].get("value") not in (None, previous_value))
        verified = bool(screen["focus"] and (moved or value_changed)) if select else not moved
        evidence = ["Focused label matched"]
        if select:
            evidence.append(
                "Accessibility value changed" if value_changed else "Selection changed focus" if verified else "Selection acknowledged; effect unconfirmed"
            )
        return outcome(
            f"{'Selected' if select else 'Focused'} {to}.", evidence, {"path": path}, verified
        ) | {"screen": screen}

    async def open_app(self, app, *, require_verified_launch=False):
        match = match_app(app, (await self.ops.tv_list_apps())["apps"])
        result = await self.ops.tv_open(app=match["bundle_id"])
        if require_verified_launch:
            checked(result)
        if result["ok"] or result["error"]["code"] == "open_unverified":
            await self.resolve_prompts()
        if not result["ok"] and result["error"]["code"] == "open_unverified":
            snapshot = await self.snapshot()
            if (snapshot["app"] or {}).get("id") == match["bundle_id"]:
                return snapshot["app"]
        checked(result)
        return {"id": match["bundle_id"], "name": match["name"]}

    async def search(self, match, query):
        recipe = RECIPES["default"] | RECIPES.get(match["name"], {})
        if template := recipe.get("search_link"):
            async with self.engine.session() as client:
                await self.ops.launch(client, template.format(query=quote(query, safe="")))
            return "search_link"
        await navigate(self.observe, self.press, recipe["search_button"], True)
        checked(await self.ops.tv_type(query))
        return "in_app_search"

    async def tv_open(self, app, search=None, path=None, *, require_verified_launch=False):
        """Launch or search an installed app, or follow a menu path. Can wake the TV and ends its bridge presentation.
        Matches an installed name or ID, checks the reported app, and uses search links, keyboard readback or accessibility for the next step.
        Inspect data.app, data.steps for a path, and screen. A search can succeed with verified=false. Use tv_play to start a title.
        For example, tv_open {"app":"Settings"}."""
        await self.takeover()
        match = await self.open_app(app, require_verified_launch=require_verified_launch)
        data = {"app": match}
        verified = True
        evidence = ["Foreground app matched"]
        if search:
            data["path"] = await self.search(match, search)
            if data["path"] == "in_app_search":
                evidence.append("Keyboard text matched the query")
            else:
                try:
                    focus, items = await self.observe()
                    verified = any(
                        normal(search) in normal(label(item)) for item in [focus, *items]
                    )
                except Fault:
                    verified = False
                evidence.append(
                    "Search text observed"
                    if verified
                    else "Search link opened; results unconfirmed"
                )
        elif path:
            if match["id"] == "com.apple.TVSettings":
                await self.settings_root()
            data.update(path=[], steps=[])
            for index, part in enumerate(path):
                action = await self.tv_navigate(part, select=True)
                data["path"].extend(action["data"]["path"])
                evidence.extend(action["evidence"])
                destination = action["screen"]
                step = {
                    "target": part,
                    "verified": action["verified"],
                    "focus_after": label(destination["focus"]) or None,
                }
                data["steps"].append(step)
                if step["verified"]:
                    observation = await self.read_observation()
                    focus = observation["focused"]
                    destination = await self.snapshot(focus, False, observation)
                    labels = [normal(label(item)) for item in observation["items"]]
                    step["focus_after"] = label(focus) or None
                    step["target_visible"] = normal(part) in [normal(label(focus)), *labels]
                    arrived = step["target_visible"] if index == len(path) - 1 else (
                        focus and normal(label(focus)) in labels
                    )
                    step["verified"] = bool(arrived and (destination["app"] or {}).get("id") == match["id"])
                if not step["verified"]:
                    raise Fault(
                        "uncertain",
                        f"Selected {part}, but the destination could not be verified.",
                        "Read tv_look before continuing the menu path.",
                        evidence=evidence,
                        **data,
                    )
        result = outcome(f"Opened {match['name']}.", evidence, data, verified=verified)
        return result | {"screen": destination} if path else result

    async def wait_playing(self, before, expected=None, title=None, airplay=False):
        deadline = asyncio.get_running_loop().time() + self.play_timeout
        while True:
            s = await self.engine.status()
            playing = s.get("now_playing") or {}
            matches_app = not expected or (s.get("app") or {}).get("bundle_id") == expected
            observed = (
                s.get("airplay_playing")
                if airplay
                else playback_identity(playing) != playback_identity(before)
                or (title and normal(title) == normal(playing.get("title")))
            )
            if (
                matches_app
                and playing.get("state") == "playing"
                and observed
                and (not title or normal(title) in normal(playing.get("title")))
            ):
                return playing
            if self.media.problem:
                raise self.media.problem
            if asyncio.get_running_loop().time() >= deadline:
                raise Fault(
                    "uncertain",
                    "The requested playback could not be verified.",
                    "Read tv_look before trying again.",
                )
            await asyncio.sleep(0.25)

    async def play_link(self, link, before):
        url, rule = link_info(link)
        targets = link_targets(url, rule)

        async def launch():
            try:
                async with self.engine.session() as client:
                    await self.ops.launch(client, url)
            except Fault as exc:
                if exc.code not in ("command_rejected", "launch_refused"):
                    raise
                raise Fault(
                    "launch_refused",
                    str(exc),
                    "For this URL, try another link form or tv_open + tv_navigate.",
                ) from exc

            if urlsplit(url).scheme not in ("http", "https"):
                await confirm_custom_link(self, targets)

        await launch()
        if await self.resolve_prompts():
            await launch()
            await self.resolve_prompts()

        deadline = asyncio.get_running_loop().time() + self.play_timeout
        while True:
            view = await observed(self)
            status = await self.engine.status()
            screen = await self.snapshot(
                view.get("focused"), False, view, status=status
            )
            app = screen["app"] or {}
            media = status.get("now_playing") or {}
            owner = (status.get("app") or {}).get("bundle_id")
            fresh_playback = media.get("state") == "playing" and playback_identity(
                media
            ) != playback_identity(before)
            foreground = app.get("id") if app.get("source") == "accessibility" else None
            # Without AX, changed playback from the expected app is independent
            # evidence. Paused or unchanged now-playing metadata can outlive the UI.
            if (
                foreground is None
                and fresh_playback
                and not (view.get("app") or view.get("focused"))
            ):
                foreground = owner
            if foreground in targets:
                evidence = [f"Observed target app {targets[foreground]}"]
                labels = {normal(label(i)) for i in view.get("items", [])}
                player_controls = bool(
                    "pause" in labels
                    and labels & {"elapsed time", "remaining time", "scrubber"}
                )
                matching_content = media.get("content_identifier") in (link, url)
                if (
                    owner == foreground
                    and media.get("state") == "playing"
                    and (fresh_playback or matching_content or player_controls)
                ):
                    landed = "playing"
                    evidence.append("Playback verified in the target app")
                elif action := title_action(view):
                    landed = "title_page"
                    evidence.append(f"Title action: {action}")
                else:
                    landed = "app_home"
                    if focused := label(view.get("focused")):
                        evidence.append(f"Focused: {focused}")
                summary = {
                    "playing": "Playback started",
                    "title_page": "Opened the title page",
                    "app_home": "Opened the app",
                }[landed]
                return outcome(
                    f"{summary} in {targets[foreground]}.",
                    evidence,
                    {"path": "deep_link", "landed": landed},
                ) | {"screen": screen}
            if asyncio.get_running_loop().time() >= deadline or not targets:
                raise Fault(
                    "uncertain",
                    "Could not verify the link’s target app in the foreground.",
                    "Read tv_look before continuing.",
                )
            await asyncio.sleep(0.25)

    async def tv_play(self, youtube=None, media=None, link=None, title=None, app=None):
        """Start a video, media URL or title, or inspect a deep link. Can wake the TV and takes over its presentation.
        Launches a link, searches the app or streams over AirPlay, then checks playback or the foreground destination. A custom link can trigger a screenshot toast.
        Inspect data.path and screen.playing. For links, verified can mean data.landed is title_page or app_home. Use tv_open for browsing.
        For example, tv_play {"youtube":"jNQXAC9IVRw"}."""
        await self.takeover()
        before = (await self.engine.status()).get("now_playing")
        expected = None
        if youtube:
            async def after_youtube_launch():
                # youtube:// links can raise tvOS's "Open in YouTube?" dialog
                # (seen on tvOS 26.6.1); accessibility text cannot see it.
                await confirm_custom_link(self, {"com.google.ios.youtube": "YouTube"})
                return await self.resolve_prompts()

            result = checked(await self.ops.tv_play_youtube(url=youtube, timeout=self.play_timeout / 2, after_launch=after_youtube_launch))
            return outcome("YouTube playback was observed.", result["evidence"], {"path": "youtube"})
        if media:
            if urlsplit(media).scheme not in ("http", "https") or not urlsplit(media).hostname:
                raise Fault(
                    "invalid_arguments",
                    "Media must be an HTTP or HTTPS URL.",
                    "Supply a receiver-accessible media URL.",
                )
            await self.ops.live_mirror.wait_for_airplay_handoff()
            await self.media.start(media)
            route = "airplay"
        elif link:
            return await self.play_link(link, before)
        else:
            match = await self.open_app(app)
            expected = match["id"]
            recipe = RECIPES["default"] | RECIPES.get(match["name"], {})
            known = recipe.get("titles", {}).get(normal(title))
            if known:
                async with self.engine.session() as client:
                    await self.ops.launch(client, recipe["title_link"].format(id=quote(known, safe="/.")))
                if await self.resolve_prompts():
                    async with self.engine.session() as client:
                        await self.ops.launch(client, recipe["title_link"].format(id=quote(known, safe="/.")))
                    await self.resolve_prompts()
                route = "title_link"
            else:
                route = await self.search(match, title)
                await navigate(self.observe, self.press, title, True)
                current = (await self.engine.status()).get("now_playing") or {}
                if current.get("state") != "playing" or normal(title) not in normal(
                    current.get("title")
                ):
                    _, items = await self.observe()
                    for button in recipe["play_buttons"]:
                        if any(normal(label(i)).startswith(normal(button)) for i in items):
                            await navigate(self.observe, self.press, button, True)
                            break
        playing = await self.wait_playing(before, expected, title, airplay=bool(media))
        return outcome(
            f"Playing {playing.get('title') or title or 'the requested media'}.",
            ["Playback state: playing"],
            {"path": route},
        )

    async def tv_hide(self):
        """Stop the current presentation (compatibility alias)."""
        return await self.tv_show()

    async def tv_show(
        self, url=None, html=None, files=None, bundle=None, title=None, audio="play", remote=None,
    ):
        """Present a page, or dismiss it by omitting the source. Can wake the TV and replaces its bridge media or page.
        The HA host renders the page and mirrors it or plays a recording. Live URLs use a mirror. The default fleet limit is one active mirror.
        Inspect data.transport and screen.presenting. verified means frames were sent or playback was observed. Use tv_page_run to read or change a live page.
        Siri Remote arrows and Select reach a mirrored page by default: remote is "tvos" for html, files and bundle, and "focus" for url. Lay out a "tvos" page in rows and grids. Set remote to "off" to send no input. data.remote.state is often "starting"; later results report it in screen.presenting.remote.
        For example, tv_show {"html":"<h1>Hello</h1>"}."""
        await self.media.stop()
        if all(source is None for source in (url, html, files, bundle)):
            await self.ops.tv_hide()
            return outcome("Stopped presenting.", ["Sender stopped"])
        if remote is None:
            # A page you write is laid out for the tvOS lift. On a live site the lift could clip or cover a neighbor.
            remote = "focus" if url is not None else "tvos"
        result = await self.ops.present(
            url=url,
            html=html,
            files=files,
            bundle=bundle,
            title=title,
            audio=audio,
            remote=remote,
        )
        mode = result["mode"]
        data = {}
        if remote != "off":
            # Remote input must not delay the show. The key setup and the watch start run in the background,
            # and later results report the state in screen.presenting.remote.
            if mode == "mirror":
                self.remote_input.prime_soon()
            status = self.remote_input.status()
            data["remote"] = {k: status[k] for k in ("mode", "state", "reason") if k in status}
        return outcome(
            "The bridge is presenting the page."
            if result["verified"]
            else "Page delivery is awaiting confirmation.",
            result["evidence"],
            {
                "page_id": result["page_id"],
                "size": self.size if mode == "mirror" else "1920x1080",
                "transport": "mirror" if mode == "mirror" else "video",
                **data,
            },
            result["verified"],
        )

    async def tv_page_run(self, script, arg=None, timeout_ms=5000):
        if not self.page_scripts:
            raise Fault("scripts_disabled", "Page script execution is disabled.", "Enable page_scripts in the app options and restart the app.")
        data = await self.page_interaction.run(script, arg, timeout_ms)
        return outcome("Ran the page script.", ["Chromium returned the script result"], data)

    tv_page_run.__doc__ = RUN_HELP
