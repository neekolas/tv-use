"""Exercise the public dispatcher against a focusable UI and fake control session."""

import asyncio
import contextlib
from unittest.mock import AsyncMock

import pytest
from aiohttp import web

from atv_bridge_app.present.sender import STAGE_URL
from atv_bridge_app.state import Fault
from atv_bridge_app.tools.contract import RETRYABLE, SCHEMAS
from atv_bridge_app.tools.navigation import navigate


class Grid:
    def __init__(self, labels, columns, frames=True):
        self.labels = labels
        self.columns = columns
        self.frames = frames
        self.cursor = 0
        self.keys = []
        self.steps = []

    async def observe(self):
        items = []
        for index, label in enumerate(self.labels):
            y, x = divmod(index, self.columns)
            item = {"caption": label, "uid": str(index), "role": "button"}
            if self.frames:
                item["frame"] = {"x": x * 100, "y": y * 100, "width": 80, "height": 80}
            items.append(item)
        self.steps.append(("read", self.cursor))
        return items[self.cursor], items

    async def press(self, key, *_):
        self.steps.append(("press", key))
        self.keys.append(key)
        row, col = divmod(self.cursor, self.columns)
        dx, dy = {"right": (1, 0), "left": (-1, 0), "down": (0, 1), "up": (0, -1), "select": (0, 0)}[
            key
        ]
        index = (row + dy) * self.columns + col + dx
        if 0 <= col + dx < self.columns and 0 <= index < len(self.labels):
            self.cursor = index

    async def observer(self, method, **kwargs):
        focus, items = await self.observe()
        if method == "ax_focused":
            return {"focused": focus}
        if method == "ax_items":
            return {"items": items, "focused": focus}
        raise AssertionError(method)


@pytest.mark.parametrize(
    "cols,frames,expected",
    [
        (3, True, ["right", "down", "right"]),
        (1, True, ["down"] * 5),
        (6, False, ["down"] + ["right"] * 5),
    ],
)
async def test_navigation_decides_again_after_each_press(cols, frames, expected):
    grid = Grid(list("ABCDEF"), cols, frames)
    assert await navigate(grid.observe, grid.press, "f", select=True) == expected + ["select"]
    assert [kind for kind, _ in grid.steps] == ["read", "press"] * (len(expected) + 1)
    assert grid.cursor == 5


@pytest.mark.parametrize("count,limit", [(1, 3), (80, 40)])
async def test_navigation_bounds(count, limit):
    grid = Grid([str(n) for n in range(count)], 1)
    with pytest.raises(Fault) as caught:
        await navigate(grid.observe, grid.press, "Absent")
    assert caught.value.code == "not_found"
    assert caught.value.fields["path"] == grid.keys and len(grid.keys) == limit


@pytest.fixture
async def ui(service):
    tv = await service.engine.ensure()
    grid = Grid(["Home", "Search", "Results", "Succession"], 2)
    service.observer.call = grid.observer
    tv.hid = grid.press
    yield service, grid, tv
    await service.tools.media.stop()
    await service.engine.stop()
    await service.observer.close()


@pytest.mark.parametrize("name", SCHEMAS)
async def test_public_invalid_arguments(service, name):
    r = await service.tools.call(name, {"extra": 1})
    assert r["error"]["code"] == "invalid_arguments"
    assert set(r["error"]) == {"code", "message", "retryable", "fix"}
    assert r["summary"] and r["evidence"] == [] and r["data"] == {}
    assert not r["ok"] and not r["verified"]
    assert ("screen" in r) == (name not in ("tv_status", "tv_history"))


async def test_open_remote_navigate_look_status(ui):
    service, grid, tv = ui
    call = service.tools.call
    r = await call("tv_open", {"app": "Youtub", "path": ["Search"]})
    assert r["data"]["app"] == {"id": "com.google.ios.youtube", "name": "YouTube"}
    assert r["data"]["path"] == ["right", "select"]
    r = await call("tv_remote", {"keys": ["down"]})
    assert r["ok"] and r["verified"] and r["screen"]["focus"]["label"] == "Succession"
    assert not (await call("tv_remote", {"keys": ["down"]}))["verified"]
    r = await call("tv_navigate", {"to": "res", "within": "main"})
    assert r["verified"] and r["data"]["path"] == ["left"]
    r = await call("tv_look", {})
    assert set(r["screen"]) == {"power", "app", "presenting", "playing", "focus", "keyboard"}
    assert [item["label"] for item in r["data"]["items"] if item["focused"]] == ["Results"]
    r = await call("tv_status", {})
    assert "screen" not in r and r["data"]["tv"]["reachable"]
    assert set(r["data"]["capabilities"]) == {"look_image", "look_text", "show", "play_media"}


async def test_select_rechecks_transition_without_sending_another_key(ui):
    service, grid, _ = ui
    service.tools.snapshot = AsyncMock(side_effect=[
        {"focus": None}, {"focus": {"label": "Clickpad", "role": None}}
    ])
    result = await service.tools.tv_navigate("Search", select=True)
    assert result["verified"] and result["screen"]["focus"]["label"] == "Clickpad"
    assert result["data"]["path"] == grid.keys == ["right", "select"]


async def test_open_match_ambiguity_and_rejection(ui):
    service, _, tv = ui
    tv.apps = AsyncMock(
        return_value=[
            {"name": "Music Radio", "bundle_id": "org.a"},
            {"name": "Music Video", "bundle_id": "org.b"},
        ]
    )
    call = service.tools.call
    ambiguous = await call("tv_open", {"app": "Music"})
    assert ambiguous["error"]["code"] == "app_not_installed" and len(ambiguous["data"]["apps"]) == 2
    assert (await call("tv_open", {"app": "XYZ123"}))["data"]["apps"] == []
    tv.open = AsyncMock(side_effect=Fault("command_rejected", "Launch refused.", "Check Restrictions."))
    assert (await call("tv_open", {"app": "org.a"}))["error"]["code"] == "launch_refused"


async def test_title_search_ignores_unrelated_subscribe_button(ui):
    service, grid, tv = ui
    tv.apps = AsyncMock(return_value=[{"name": "HBO Max", "bundle_id": "com.max"}])
    tv.type = AsyncMock(return_value=True)

    async def select(key, hold):
        await grid.press(key)
        if key == "select" and grid.labels[grid.cursor] == "Succession":
            tv.media = {"state": "playing", "title": "Succession"}

    tv.hid = select
    r = await service.tools.call("tv_play", {"title": "Succession", "app": "HBO Max"})
    assert r["ok"] and r["verified"] and r["data"]["path"] == "in_app_search"
    assert tv.type.await_args.args == ("Succession", True)
    grid.labels[0] = "Subscribe"
    tv.media = None
    grid.keys.clear()
    r = await service.tools.call("tv_play", {"title": "Succession", "app": "HBO Max"})
    assert r["ok"] and r["verified"]
    assert "select" in grid.keys


async def test_play_youtube_and_link_require_new_playback(ui):
    service, _, tv = ui
    r = await service.tools.call("tv_play", {"youtube": "https://youtu.be/dQw4w9WgXcQ"})
    assert r["verified"] and r["screen"]["playing"]["state"] == "playing"
    assert len(tv.targets) == 2

    # An advancing playback clock for the old video is insufficient.
    async def old_video(_):
        tv.media["position_s"] = 10

    tv.open = old_video
    failed = await service.tools.call("tv_play", {"link": "youtube://watch/abcdefghijk"})
    assert failed["error"]["code"] == "uncertain"
    assert (
        await service.tools.call(
            "tv_play", {"app": "TV", "title": "XYZ", "media": "https://local.invalid/x"}
        )
    )["error"]["code"] == "invalid_arguments"


async def test_airplay_interrupt_reopens_and_eof_finishes(ui):
    service, _, tv = ui
    service.tools.play_timeout = 2
    service.engine.adapter.supports_play_url = True
    status = tv.status
    positions = []
    finished = asyncio.Event()
    playing = False

    async def live_status():
        return await status() | {"airplay_playing": playing}

    async def play(url, position):
        nonlocal playing
        positions.append(position)
        playing = True
        tv.media = {
            "state": "playing",
            "position_s": 30 if len(positions) == 1 else 300,
            "duration_s": 300,
        }
        if len(positions) == 1:
            await asyncio.sleep(0.55)
            return True
        await finished.wait()
        return False

    connect = service.engine.adapter.connect

    async def reconnect(*args, **kwargs):
        fresh = await connect(*args, **kwargs)
        fresh.status, fresh.play_url = live_status, play
        return fresh

    service.engine.adapter.connect = reconnect
    tv.status, tv.play_url = live_status, play
    result = await service.tools.call("tv_play", {"media": "https://example.invalid/video.mp4"})
    assert result["verified"] and result["data"]["path"] == "airplay"
    async with asyncio.timeout(4):
        while len(positions) < 2:
            await asyncio.sleep(0.05)
        finished.set()
        await service.tools.media.task
    assert positions == [0, 30] and service.tools.media.problem is None


async def test_remote_bursts_keys_and_reads_once(ui):
    service, grid, tv = ui
    grid.steps.clear()
    r = await service.tools.call("tv_remote", {"keys": ["right", "down", "left"]})
    assert r["ok"] and r["verified"] and r["data"]["keys"] == ["right", "down", "left"]
    presses = [i for i, step in enumerate(grid.steps) if step[0] == "press"]
    assert presses == [presses[0], presses[0] + 1, presses[0] + 2]
    assert r["screen"]["focus"]["label"] == "Results"


async def test_remote_reuses_prompt_check_read_as_starting_screen(ui):
    service, grid, tv = ui
    methods = []

    async def counted(method, **kwargs):
        methods.append(method)
        return await grid.observer(method, **kwargs) | {"app": {"bundle_id": "com.example.app", "name": "Example"}}

    service.state.save_pairings(remote_pairing=True)
    service.observer.call = counted
    r = await service.tools.call("tv_remote", {"keys": ["right"]})
    assert r["ok"] and r["verified"] and r["screen"]["focus"]["label"] == "Search"
    assert methods == ["ax_items", "ax_focused"]


async def test_remote_burst_ignores_unreadable_focus_but_check_each_stops(ui):
    service, grid, tv = ui
    reads = {"n": 0}

    async def blind(method, **kwargs):
        reads["n"] += 1
        return {"focused": None}

    service.observer.call = blind
    r = await service.tools.call("tv_remote", {"keys": ["right", "down"]})
    assert r["ok"] and grid.keys == ["right", "down"]
    grid.keys.clear()
    r = await service.tools.call("tv_remote", {"keys": ["right", "down"], "check_each": True})
    assert r["error"]["code"] == "uncertain" and r["data"]["confirmed_keys"] == 1
    assert grid.keys == ["right"]


async def test_remote_rereads_after_home_before_more_keys(ui):
    service, grid, tv = ui
    service.tools.ops.changing_app = lambda: contextlib.nullcontext()

    async def press(key, hold=0):
        if key == "home":
            grid.steps.append(("press", key))
            grid.keys.append(key)
            return
        await grid.press(key)

    tv.hid = press
    grid.steps.clear()
    r = await service.tools.call("tv_remote", {"keys": ["home", "right"]})
    assert r["ok"] and grid.keys == ["home", "right"]
    kinds = [step[0] for step in grid.steps]
    assert "read" in kinds[kinds.index("press") : len(kinds) - kinds[::-1].index("press")]


async def test_remote_uncertain_no_text_field_and_observer_failure(ui):
    service, grid, tv = ui

    async def lose(key, hold):
        await grid.press(key)
        if key == "select":
            raise OSError("lost reply")

    tv.hid = lose
    r = await service.tools.call("tv_remote", {"keys": ["right", "select", "down"]})
    assert r["error"]["code"] == "uncertain" and r["data"]["confirmed_keys"] == 1
    assert grid.keys == ["right", "select"]
    service.engine.reset()
    tv = await service.engine.ensure()
    tv.type = AsyncMock(
        side_effect=Fault("keyboard_unfocused", "No text field.", "Focus a text field.")
    )
    assert (await service.tools.call("tv_remote", {"text": "words"}))["error"][
        "code"
    ] == "no_text_field"
    service.observer.call = AsyncMock(
        side_effect=Fault("observer_failed", "Missing observer.", "Pair Remote.")
    )
    assert (await service.tools.call("tv_look", {}))["error"]["code"] == "screen_unavailable"


@pytest.mark.parametrize(
    "name,args",
    [
        ("tv_open", {"app": "TV"}),
        ("tv_show", {}),
        ("tv_hide", {}),
        ("tv_navigate", {"to": "TV"}),
        ("tv_remote", {"swipe": "left"}),
        ("tv_play", {"youtube": "123"}),
        ("tv_power", {"on": False}),
    ],
)
async def test_every_mutation_checks_readiness(ui, name, args):
    service, grid, tv = ui
    service.engine.ready = AsyncMock(
        return_value=Fault("wake_failed", "Did not wake.", "Press the remote.").result()
    )
    result = await service.tools.call(name, args)
    assert result["error"]["code"] == "tv_unreachable" and not grid.keys and not tv.targets


@pytest.mark.parametrize(
    "internal,code",
    [
        ("pairing_required", "not_paired"),
        ("pairing_removed", "pairing_removed"),
        ("allow_access_changed", "access_blocked"),
    ],
)
async def test_readiness_reports_specific_fix(ui, internal, code):
    service, _, _ = ui
    service.engine.ready = AsyncMock(
        return_value=Fault(internal, "Cannot connect.", "Check receiver settings.").result()
    )
    result = await service.tools.call("tv_open", {"app": "TV"})
    assert result["error"] == {
        "code": code,
        "retryable": code in RETRYABLE,
        "message": "Cannot connect.",
        "fix": "Check receiver settings.",
    }


async def test_contention_returns_busy_then_releases(ui):
    service, grid, _ = ui
    started, release = asyncio.Event(), asyncio.Event()

    async def blocked(*args, **kwargs):
        started.set()
        await release.wait()
        return await grid.observer(*args, **kwargs)

    service.observer.call = blocked
    assert service.tools.wait_timeout == 10
    service.tools.wait_timeout = 0.02
    first = asyncio.create_task(service.tools.call("tv_look", {}))
    try:
        await asyncio.wait_for(started.wait(), 1)
        r = await service.tools.call("tv_remote", {"keys": ["right"]})
        assert r["error"]["code"] == "busy" and not grid.keys
    finally:
        release.set()
        assert (await first)["ok"]
    assert (await service.tools.call("tv_remote", {"keys": ["right"]}))["ok"]


async def test_power_auto_rebuild_and_confirm_sleep(service):
    service.observer.call = AsyncMock(return_value={"focused": None})
    r = await service.tools.call("tv_power", {"on": True})
    assert service.engine.adapter.clients[0].keys == ["wake"] and r["screen"]["power"] == "on"
    tv = service.engine.client

    async def sleep(key, hold):
        assert key == "sleep"
        tv.power_state = "off"

    tv.hid = sleep
    assert (await service.tools.call("tv_power", {"on": False}))["screen"]["power"] == "off"


@pytest.mark.parametrize(
    "policy,timeout,top_level",
    [
        ({}, False, False),
        ({"X-Frame-Options": "DENY"}, False, True),
        ({"Content-Security-Policy": "frame-ancestors 'none'"}, False, True),
        ({}, True, True),
    ],
)
async def test_live_page_frame_fallback_and_return(
    ui, aiohttp_server, policy, timeout, top_level
):
    service, _, _ = ui

    async def site(request):
        return web.Response(text="<h1>Live</h1>", headers=policy, content_type="text/html")

    app = web.Application()
    app.router.add_get("/", site)
    server = await aiohttp_server(app)
    service.stage.stage_ports.add(server.port)
    url = str(server.make_url("/"))
    mirror = service.operations.live_mirror
    mirror.start = AsyncMock(side_effect=[TimeoutError(), None, None] if timeout else None)
    mirror.verify_document = AsyncMock()
    mirror.confirm_frame = AsyncMock(return_value=True)
    call = service.tools.call
    r = await call("tv_show", {"url": url, "audio": "mute"})
    assert r["verified"] and r["data"]["transport"] == "mirror" and mirror.audio == "mute"
    assert [args.args[0] for args in mirror.start.await_args_list] == (
        [STAGE_URL, url] if top_level else [STAGE_URL]
    )
    # A live site gets focus by default, framed or at the top level.
    assert r["data"]["remote"]["mode"] == service.stage.page["remote"] == "focus"
    mirror.confirm_frame.return_value = False
    r = await call("tv_show", {"files": {"index.html": "<h1>Next</h1>", "img.png": "base64:aGk="}})
    assert (
        r["ok"] and not r["verified"] and r["screen"]["presenting"]["page_id"] == r["data"]["page_id"]
    )
    assert r["data"]["remote"]["mode"] == service.stage.page["remote"] == "tvos"
    assert service.stage.store.file(r["data"]["page_id"], "img.png").read_bytes() == b"hi"
    assert mirror.start.await_args.args == (STAGE_URL,) and service.stage.direct_browser is None
    assert (await call("tv_show", {}))["screen"]["presenting"] is None


async def test_show_rejects_keep_awake(service):
    """Hardware finding: the wake lock stayed in the host's headless Chromium and never reached the TV.
    An active mirror alone kept tvOS awake for 18 minutes, past its screensaver and sleep timers."""
    r = await service.tools.call("tv_show", {"html": "<h1>x</h1>", "keep_awake": True})
    assert r["error"]["code"] == "invalid_arguments" and service.stage.page is None


async def test_missing_bundle_bad_inline_and_http_error(ui, aiohttp_server):
    service, _, _ = ui
    call = service.tools.call
    assert (await call("tv_show", {"bundle": "missing"}))["error"]["code"] == "page_unreachable"
    assert (await call("tv_show", {"files": {"index.html": "page", "../escape": "x"}}))["error"][
        "code"
    ] == "invalid_arguments"
    assert (await call("tv_show", {"files": {"index.html": "base64::bad"}}))["error"][
        "code"
    ] == "invalid_arguments"

    async def unavailable(request):
        raise web.HTTPServiceUnavailable()

    app = web.Application()
    app.router.add_get("/missing", unavailable)
    server = await aiohttp_server(app)
    service.stage.stage_ports.add(server.port)
    assert (await call("tv_show", {"url": str(server.make_url("/missing"))}))["error"][
        "code"
    ] == "page_unreachable"


async def test_magicdns_failure_visible_in_health(ui, monkeypatch):
    service, _, _ = ui
    import socket

    resolver = AsyncMock(side_effect=socket.gaierror(-2, "no DNS record"))
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolver)
    url = "https://fake.example.ts.net:8443"
    r = await service.tools.call("tv_show", {"url": url})
    assert r["error"]["code"] == "page_unreachable"
    health = await service.operations.tv_health()
    assert "no DNS record" in r["error"]["message"]
    assert (
        r["error"]["fix"] == "Set HA DNS to Tailscale: ha dns options --servers dns://100.100.100.100"
    )
    assert health["settings"]["magicdns"] == {"resolved": False, "hostname": "fake.example.ts.net"}
    status = await service.tools.call("tv_status", {})
    assert any(i["code"] == "page_unreachable" for i in status["data"]["problems"])
    resolver.side_effect = None
    assert (await service.operations.tv_health())["settings"]["magicdns"]["resolved"] is True


async def test_known_catalog_title_and_search_link(ui):
    service, grid, tv = ui
    tv.apps = AsyncMock(return_value=[{"name": "TV", "bundle_id": "com.apple.TV"}])

    async def open_title(target):
        tv.app = (await tv.apps())[0]
        tv.targets.append(target)
        if target.startswith("https://"):
            tv.media = {"state": "playing", "title": "Severance"}

    tv.open = open_title
    r = await service.tools.call("tv_play", {"title": "Severance", "app": "TV"})
    assert r["verified"] and r["data"]["path"] == "title_link"
    assert tv.targets[-1].endswith("umc.cmc.1srk2goyh2q2zdxcx605w8vtx?action=play")
    assert not grid.keys
    tv.apps = AsyncMock(return_value=[{"name": "YouTube", "bundle_id": "com.google.ios.youtube"}])
    r = await service.tools.call("tv_open", {"app": "YouTube", "search": "live music"})
    assert r["ok"] and not r["verified"] and tv.targets[-1].endswith("search_query=live%20music")


async def test_swipe_and_submit_return_evidence(ui):
    service, grid, tv = ui

    async def swipe(direction):
        await grid.press(direction)

    tv.swipe = swipe
    call = service.tools.call
    assert (await call("tv_remote", {"swipe": "right"}))["screen"]["focus"]["label"] == "Search"
    tv.type = AsyncMock(return_value=True)
    r = await call("tv_remote", {"text": "Example", "submit": True})
    assert tv.type.await_args.args == ("Example", True)
    assert r["data"]["keys"] == ["select"] and grid.keys == ["right", "select"] and not r["verified"]


async def test_protocol_normal_eof_ends_even_with_stale_duration(ui):
    service, _, tv = ui
    service.engine.adapter.supports_play_url = True
    tv.play_url = AsyncMock(return_value=False)
    await service.tools.media.start("https://example.invalid/movie")
    await asyncio.wait_for(service.tools.media.task, 1)
    tv.play_url.assert_awaited_once()
    assert service.tools.media.problem is None


async def test_menu_path_stops_at_unverified_selection(ui):
    service, grid, _ = ui
    result = await service.tools.call("tv_open", {"app": "YouTube", "path": ["Search", "Results"]})
    assert result["error"]["code"] == "uncertain" and not result["verified"]
    assert result["data"]["steps"] == [
        {"target": "Search", "verified": False, "focus_after": "Search"}
    ]
    assert grid.keys == ["right", "select"]


@pytest.mark.parametrize("correct_destination", [True, False])
async def test_nested_settings_path_observes_each_destination(ui, correct_destination):
    service, _, tv = ui
    remotes, devices = "Remotes and Devices", "Remote App and Devices"
    settings = {"bundle_id": "com.apple.TVSettings", "name": "Settings"}
    tv.apps = AsyncMock(return_value=[settings])
    tv.open = AsyncMock()
    tv.app = {"bundle_id": "com.hulu.plus", "name": "Hulu"}
    tv.media = {"title": "Paused episode", "state": "paused"}
    service.state.save_pairings(remote_pairing=True)
    pages = [("Settings", remotes), (remotes, devices), (devices if correct_destination else "Unrelated menu", "Paired remote")]
    level = 0

    async def observe(method, **kwargs):
        heading, row = pages[level]
        focused = {"label": row, "uid": "row"}
        observation = {"focused": focused, "app": settings}
        if method == "ax_items":
            observation["items"] = [{"label": heading, "uid": "heading"}, focused]
            if level == 0:
                observation["items"].append({"label": "Network", "uid": "network"})
        else:
            assert method == "ax_focused"
        return observation

    async def press(key, hold_ms):
        nonlocal level
        assert key == "select" and level < 2
        level += 1

    service.observer.call = observe
    tv.hid = press
    result = await service.tools.call("tv_open", {"app": "Settings", "path": [remotes, devices]})
    if not correct_destination:
        assert level == 2 and not result["ok"] and not result["verified"]
        assert result["error"]["code"] == "uncertain"
        assert not result["data"]["steps"][-1]["target_visible"]
        return
    assert result["ok"] and result["verified"] and level == 2
    assert result["data"]["steps"] == [
        {"target": remotes, "focus_after": devices, "verified": True, "target_visible": True},
        {"target": devices, "focus_after": "Paired remote", "verified": True, "target_visible": True},
    ]
    assert result["screen"]["app"]["id"] == settings["bundle_id"]
    assert result["screen"]["playing"]["title"] == "Paused episode"

async def test_invalid_hold_rejects_entire_gesture_before_ready(service):
    ready = service.engine.ready = AsyncMock()
    press = service.tools.press = AsyncMock()
    result = await service.tools.call("tv_remote", {"keys": ["down", "pause"], "hold_ms": 100})
    assert result["error"]["code"] == "invalid_arguments"
    ready.assert_not_awaited()
    press.assert_not_awaited()


@pytest.mark.parametrize("args", [
    {"steps": [{"key": "right"}, {"expect": {}}]},
    {"steps": [{"key": "right"}, {"to": "Search", "key": "select"}]},
    {"steps": [{"key": "right"}, {"expect": {"focus": "$item"}}]},
    {"steps": [{"key": "right"}] * 2, "for_each": ["X"] * 26},
])
async def test_sequence_validates_whole_request_before_wake(ui, args):
    service, grid, _ = ui
    service.engine.ready = AsyncMock()
    result = await service.tools.call("tv_sequence", args)
    assert result["error"]["code"] == "invalid_arguments"
    service.engine.ready.assert_not_awaited()
    assert not grid.steps


async def test_sequence_repeats_and_stops_at_first_mismatch(ui):
    service, grid, _ = ui
    result = await service.tools.call("tv_sequence", {
        "for_each": ["Search", "Results"],
        "steps": [{"to": "$item"}, {"expect": {"visible": " home ", "focus": "$item"}},
                  {"expect": {"focus": "Search"}}],
    })
    assert result["error"]["code"] == "not_found"
    assert result["data"]["stopped_at"] == {"iteration": 1, "step": 2}
    rows = result["data"]["steps"]
    assert [r["ok"] for r in rows] == [True] * 5 + [False]
    assert rows[-1]["data"]["observed"] == result["screen"]["focus"]["label"] == "Results"
    assert grid.keys == ["right", "down", "left"]


async def test_sequence_fast_keys_read_only_at_checkpoint(ui):
    service, grid, _ = ui
    result = await service.tools.call("tv_sequence", {
        "steps": [{"key": "right"}, {"key": "down"}, {"expect": {"focus": "Succession"}}],
    })
    assert result["ok"] and not result["verified"]
    assert [r["verified"] for r in result["data"]["steps"]] == [False, False, True]
    assert grid.steps == [("press", "right"), ("press", "down"), ("read", 3)]
    assert result["screen"]["focus"]["label"] == "Succession"


async def test_sequence_lost_key_is_not_replayed(ui):
    service, grid, tv = ui
    async def lose_reply(key, hold):
        await grid.press(key)
        if key == "select":
            raise OSError("lost reply")
    tv.hid = lose_reply
    result = await service.tools.call("tv_sequence", {
        "steps": [{"key": "right"}, {"key": "select"}, {"key": "down"}],
    })
    assert grid.keys == ["right", "select"]
    assert result["error"]["code"] == "uncertain"
    assert result["data"]["steps"][0]["ok"] and not result["data"]["steps"][1]["ok"]
    assert result["data"]["stopped_at"] == {"iteration": 0, "step": 1}


async def test_sequence_unverified_select_stops_before_next_key(ui):
    service, grid, _ = ui
    result = await service.tools.call("tv_sequence", {"steps": [{"to": "Search", "select": True}, {"key": "down"}]})
    assert result["error"]["code"] == "uncertain"
    assert grid.keys == result["data"]["steps"][0]["data"]["path"] == ["right", "select"]
    grid.keys.clear()
    result = await service.tools.call("tv_sequence", {"steps": [{"to": "Sea", "select": True}]})
    assert result["error"]["code"] == "not_found" and "select" not in grid.keys


async def test_sequence_checks_app_and_focused_value(ui):
    service, _, tv = ui
    view = {"focused": {"label": "Captions", "value": "On", "bundle_id": "com.apple.TVSettings"},
            "items": [{"label": "Captions"}]}
    service.state.save_pairings(remote_pairing={"paired": True})
    service.observer.call = AsyncMock(return_value=view)
    tv.type = AsyncMock(return_value=True)
    steps = [{"pause_ms": 1}, {"text": "Example"}, {"expect": {"app": "settings", "value": "ON"}}]
    result = await service.tools.call("tv_sequence", {"steps": steps})
    assert result["verified"] and result["screen"]["focus"]["value"] == "On"
    tv.type.assert_awaited_once_with("Example", True)
    view["focused"].pop("bundle_id")
    tv.app = {"name": "Settings", "bundle_id": "com.apple.TVSettings"}
    result = await service.tools.call("tv_sequence", {"steps": [steps[-1], {"key": "select"}]})
    assert result["error"]["code"] == "not_found" and not tv.keys


async def test_sequence_timeout_keeps_lock_and_reports_inflight_step(ui, monkeypatch):
    service, grid, tv = ui
    started = asyncio.Event()
    async def blocked(key, hold):
        await grid.press(key)
        started.set()
        await asyncio.Event().wait()
    tv.hid = blocked
    monkeypatch.setattr("atv_bridge_app.tools.sequence.TIME_LIMIT", 0.15)
    service.tools.wait_timeout = 0.01
    task = asyncio.create_task(service.tools.call("tv_sequence", {"steps": [{"key": "right"}, {"key": "select"}]}))
    await asyncio.wait_for(started.wait(), 1)
    assert (await service.tools.call("tv_look", {}))["error"]["code"] == "busy"
    result = await task
    assert result["error"]["code"] == "uncertain" and grid.keys == ["right"]
    assert result["data"]["stopped_at"] == {"iteration": 0, "step": 0}
    assert result["screen"]["focus"]["label"] == "Search"
    assert (await service.tools.call("tv_look", {}))["ok"]


async def test_sequence_open_path_and_final_snapshot(ui):
    service, grid, tv = ui
    async def open_menu(key, hold):
        await grid.press(key)
        if key == "select":
            grid.labels = ["Search", "Query"]
            grid.cursor = 1
    tv.hid = open_menu
    result = await service.tools.call("tv_sequence", {"steps": [{"app": "YouTube", "path": ["Search"]}]})
    assert result["ok"] and result["verified"]
    assert tv.targets == ["com.google.ios.youtube"]
    assert result["data"]["steps"][0]["data"]["steps"][0]["verified"]


async def test_sequence_checkpoint_does_not_fill_missing_focus_from_later_read(ui):
    service, grid, _ = ui
    service.observer.call = AsyncMock(side_effect=[
        {"items": [{"label": "Search"}]},
        {"focused": {"label": "Search"}},
    ])
    result = await service.tools.call("tv_sequence", {"steps": [{"expect": {"focus": "Search"}}, {"key": "select"}]})
    assert result["error"]["code"] == "not_found" and result["screen"]["focus"] is None
    service.observer.call.assert_awaited_once_with("ax_items", max_items=100)
    assert not grid.keys


async def test_sequence_unverified_open_stops_before_prompt_input(ui):
    service, grid, _ = ui
    service.operations.tv_open = AsyncMock(return_value=Fault(
        "open_unverified", "Foreground app did not match.", "Check tv_look."
    ).result())
    service.tools.resolve_prompts = AsyncMock()
    result = await service.tools.call("tv_sequence", {"steps": [{"app": "YouTube"}, {"key": "select"}]})
    assert result["error"]["code"] == "uncertain" and not grid.keys
    service.tools.resolve_prompts.assert_awaited_once_with(system=True)


async def test_sequence_bounds_final_read_and_preserves_step_history(ui, monkeypatch):
    service, grid, _ = ui
    async def stalled(*args, **kwargs):
        await asyncio.Event().wait()
    monkeypatch.setattr("atv_bridge_app.tools.sequence.SCREEN_TIMEOUT", 0.01)
    service.tools.snapshot = stalled
    result = await service.tools.call("tv_sequence", {"steps": [{"key": "right"}]})
    assert result["error"]["code"] == "screen_unavailable" and result["screen"]["power"] == "unknown"
    assert result["data"]["steps"][0]["ok"] and grid.keys == ["right"]
    assert result["data"]["stopped_at"] == {"phase": "screen"}
    assert not service.tools.lock.locked()


async def test_hung_tool_returns_uncertain_and_frees_the_lock(ui, caplog):
    service, grid, tv = ui
    service.tools.tool_timeout = 0.2

    async def hang(*args, **kwargs):
        await asyncio.sleep(60)

    service.tools.tv_navigate = hang
    with caplog.at_level("INFO"):
        r = await service.tools.call("tv_navigate", {"to": "Search"})
    assert r["error"]["code"] == "uncertain" and "did not finish" in r["error"]["message"]
    assert not service.tools.lock.locked()
    assert "Tool tv_navigate finished: uncertain" in caplog.text
    service.tools.tool_timeout = 150
    assert (await service.tools.call("tv_remote", {"keys": ["right"]}))["ok"]


@pytest.mark.parametrize("args", [{}, {"position_s": -1}, {"offset_s": 0}, {"offset_s": 1.5},
                                  {"position_s": 0, "offset_s": 5}])
async def test_seek_rejects_invalid_input(service, args):
    assert (await service.tools.call("tv_seek", args))["error"]["code"] == "invalid_arguments"


async def test_seek_readback(ui):
    service, _, tv = ui
    tv.media = {"title": "Clip", "state": "paused", "position_s": 60, "duration_s": 150}
    service.operations.verify_timeout = 0

    async def seek(position_s, offset_s):
        tv.media["position_s"] = position_s if position_s is not None else tv.media["position_s"] + offset_s

    tv.seek = AsyncMock(side_effect=seek)
    result = await service.tools.call("tv_seek", {"offset_s": -30})
    assert result["verified"] and result["data"] == {"position_s": 30, "target_s": 30}
    result = await service.tools.call("tv_seek", {"position_s": 0})
    assert result["verified"] and result["screen"]["playing"]["position_s"] == 0
    tv.seek = AsyncMock()
    assert not (await service.tools.call("tv_seek", {"position_s": 19}))["verified"]
    tv.seek = AsyncMock(side_effect=ConnectionResetError())
    assert (await service.tools.call("tv_seek", {"offset_s": -5}))["error"]["code"] == "uncertain"
    tv.seek.assert_awaited_once_with(None, -5)


async def test_volume_confirms_observed_output(ui):
    service, _, tv = ui
    tv.set_volume = AsyncMock(return_value=None)
    status = await tv.status()
    tv.status = AsyncMock(return_value=status | {"volume_percent": 24.9})
    result = await service.tools.call("tv_volume", {"percent": 25})
    assert result["verified"] and result["data"]["percent"] == 24.9
    tv.set_volume.assert_awaited_once_with(25)
    tv.status.return_value["volume_percent"] = 50
    assert not (await service.tools.call("tv_volume", {"percent": 25}))["verified"]


async def test_stop_cancels_owned_airplay_or_reports_unsupported(ui):
    service, _, tv = ui
    service.engine.adapter.supports_play_url = True
    started = asyncio.Event()
    async def play(*args):
        started.set()
        await asyncio.Event().wait()
    tv.play_url = play
    await service.tools.media.start("https://example.invalid/clip")
    await started.wait()
    assert (await service.operations.status())["playback_controls"]["stop"]
    assert (await service.tools.call("tv_remote", {"keys": ["stop"]}))["ok"]
    assert not service.tools.media.active
    tv.hid = AsyncMock(side_effect=Fault("unsupported_action", "Stop unavailable.", "Check tv_status."))
    result = await service.tools.call("tv_remote", {"keys": ["stop"]})
    assert result["error"]["code"] == "unsupported_action"
    assert result["data"]["confirmed_keys"] == 0


@pytest.mark.parametrize("state", ["playing", "paused"])
async def test_skip_after_seek_uses_current_position(ui, state):
    service, _, tv = ui
    tv.media = {"state": state, "position_s": 120, "duration_s": 240}
    status = await tv.status()
    tv.status = AsyncMock(return_value=status | {"playback_controls": {"seek_absolute": True}})
    async def seek(position_s, offset_s):
        tv.media["position_s"] = position_s
    tv.seek = AsyncMock(side_effect=seek)
    result = await service.tools.call("tv_seek", {"offset_s": 30})
    tv.seek.assert_awaited_once_with(150, None)
    assert result["verified"] and result["data"]["position_s"] == 150


async def test_stop_waits_for_player_state(ui):
    service, _, tv = ui
    service.operations.verify_timeout = 1
    tv.media = {"state": "playing"}

    async def stop(key, hold):
        assert key == "stop"
        asyncio.get_running_loop().call_later(0.25, tv.media.update, {"state": "idle"})
    tv.hid = stop
    result = await service.tools.call("tv_remote", {"keys": ["stop"]})
    assert result["verified"] and result["screen"]["playing"]["state"] == "idle"


async def test_ensure_selected_checks_the_row_independently_of_focus(ui):
    service, grid, tv = ui
    grid.labels = ["Off", "English (CC)"]
    selected = 0

    async def observe(method, **kwargs):
        focus, items = await grid.observe()
        for i, row in enumerate(items):
            row.update(pid=11, selected=i == selected, on_screen=True)
        return {"focused": focus, "items": items}

    async def press(key, hold):
        nonlocal selected
        await grid.press(key)
        if key == "select":
            selected = grid.cursor
            grid.cursor = 0

    service.observer.call = observe
    tv.hid = press
    service.tools.prepare_action = AsyncMock(side_effect=AssertionError("unexpected prompt handling"))
    args = {"to": "English (CC)", "ensure_selected": True}
    first = await service.tools.call("tv_navigate", args)
    assert first["verified"] and first["data"]["path"] == ["right", "select"]
    look = await service.tools.call("tv_look", {})
    assert [(r["focused"], r["selected"]) for r in look["data"]["items"]] == [(True, False), (False, True)]
    again = await service.tools.call("tv_navigate", args)
    assert again["verified"] and again["data"]["path"] == []
    assert grid.keys == ["right", "select"]


@pytest.mark.parametrize("case", ["duplicate", "unknown", "truncated", "scope", "prefix", "replaced", "conflict", "covered"])
async def test_ensure_selected_requires_one_observable_option(ui, case):
    service, grid, _ = ui
    reads = 0

    async def observe(method, **kwargs):
        nonlocal reads
        reads += 1
        row = {"uid": "a", "pid": 12 if case == "replaced" and reads > 1 else 11,
               "label": "English (CC)", "on_screen": case != "covered", "selected": None if case == "unknown" else False}
        rows = [row, dict(row, uid="b")] if case == "duplicate" else [row]
        return {"focused": row, "items": rows, "truncated": case == "truncated"}

    service.observer.call = observe
    args = {"to": "English" if case == "prefix" else "English (CC)", "ensure_selected": True}
    if case == "scope":
        args["within"] = "Audio"
    if case == "conflict":
        args["select"] = True
    result = await service.tools.call("tv_navigate", args)
    assert not result["ok"] and not result["verified"] and grid.keys == []


@pytest.mark.parametrize("after", ["picker_closed", "replacement_selected", "read_failed", "timed_out"])
async def test_ensure_selected_preserves_unverified_press(ui, after):
    service, grid, _ = ui
    row = {"label": "English (CC)", "uid": "a", "pid": 11, "selected": False, "on_screen": True}

    async def observe(method, **kwargs):
        item = row
        if grid.keys:
            if after == "read_failed":
                raise Fault("observer_failed", "Disconnected", "Reconnect")
            if after == "timed_out" and method == "ax_items":
                await asyncio.sleep(10)
            item = dict(row, uid="b", selected=True) if after == "replacement_selected" else None
        return {"focused": item, "items": [item] if item else []}

    service.observer.call = observe
    service.tools.settle_ms = 0
    if after == "timed_out":
        service.tools.tool_timeout = 0.2
    result = await service.tools.call("tv_navigate", {"to": "English (CC)", "ensure_selected": True})
    assert not result["verified"] and result["data"]["path"] == grid.keys == ["select"]


async def test_ensure_selected_rechecks_before_pressing(ui):
    service, grid, _ = ui
    reads = 0

    async def observe(method, **kwargs):
        nonlocal reads
        reads += 1
        option = {"label": "English (CC)", "pid": 11, "uid": "a", "on_screen": True,
                  "selected": reads >= 2}
        return {"focused": option, "items": [option]}

    service.observer.call = observe
    result = await service.tools.call("tv_navigate", {"to": "English (CC)", "ensure_selected": True})
    assert result["verified"] and result["data"]["path"] == grid.keys == []
