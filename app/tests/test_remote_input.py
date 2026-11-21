"""Remote presses: log matching, the observer watch, event demux, lifecycle and key dispatch."""

import asyncio
import json
import plistlib
import re
import struct
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx2
import pytest
from aiohttp import web
from jsonschema import Draft202012Validator
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from atv_bridge_app.present import remote_input
from atv_bridge_app.present.remote_input import FOCUS_HELPER, FOCUS_STAGE, PRESS_LOG, TVOS_HELPER
from atv_bridge_app.processes import ObserverClient
from atv_bridge_app.state import Fault, State
from atv_bridge_app.tools.contract import PAGE_GUIDE, ROUTED_SCHEMAS, SCHEMAS, canonical
from atv_bridge_app.tools.public import PublicTools
from atv_bridge_observer import __main__ as observer_module
from atv_bridge_observer import buttons, touch

FIXTURES = Path(__file__).parent / "fixtures"
LEFT = "Unknown page:0x1 usage:0x8B downEvent:1 source:externalAccessory (MediaRemote) senderID:100003D02 down"


async def until(condition, timeout=2):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.005)


# --- Matcher ---------------------------------------------------------------


@pytest.mark.parametrize(
    "message,expected",
    [
        (LEFT, ("left", True)),
        (LEFT.replace("0x8B", "0x8A"), ("right", True)),
        (LEFT.replace("0x8B", "0x8C"), ("up", True)),
        (LEFT.replace("0x8B", "0x8D"), ("down", True)),
        ("Select page:0xC usage:0x80 downEvent:1 source:externalAccessory (MediaRemote) senderID:100003D02 down", ("select", True)),
        # A physical remote reports another source and sender. The match ignores both.
        ("Unknown page:0x1 usage:0x8b downEvent:1 source:HIDDevice senderID:F00D down", ("left", True)),
        ("Select page:0x0C usage:0x80 downEvent:1", ("select", True)),
        # Lines logged by a physical clickpad Siri Remote on tvOS 27.2.
        ("Left page:0xC usage:0x44 downEvent:1 source:verifiedExternalAccessory senderID:100000597 down", ("left", True)),
        ("Right page:0xC usage:0x45 downEvent:1 source:verifiedExternalAccessory senderID:100000597 down", ("right", True)),
        ("Up page:0xC usage:0x42 downEvent:1 source:verifiedExternalAccessory senderID:100000597 down", ("up", True)),
        ("Down page:0xC usage:0x43 downEvent:1 source:verifiedExternalAccessory senderID:100000597 down", ("down", True)),
        # Each press also logs an up line and two follow-up lines.
        (LEFT.replace("downEvent:1", "downEvent:0").replace("D02 down", "D02 up"), (None, True)),
        (" 0x1/0x8B/100003D02 began: firstDown:0s ago b0e0u0 destinations:<keyboardFocus>", (None, True)),
        (" 0x1/0x8B/100003D02 finished: firstDown:0.00454s ago b0e0u1", (None, True)),
        # Menu and TV, logged by the same remote on 2026-11-21. tvOS handles them. The app never forwards them.
        ("Menu page:0x1 usage:0x86 downEvent:1 source:verifiedExternalAccessory senderID:100000597 down", ("menu", True)),
        ("TV page:0xC usage:0x60 downEvent:1 source:verifiedExternalAccessory senderID:100000597 down", ("home", True)),
        # Unknown keys and unknown line shapes are reported for diagnosis, never as a key.
        ("Unknown page:0xC usage:0xCD downEvent:1 source:externalAccessory (MediaRemote) senderID:1 down", (None, False)),
        ("Unknown page:0xC usage:0xCD downEvent:0 source:x senderID:1 up", (None, True)),
        ("A new line shape", (None, False)),
    ],
)
def test_press_matcher_uses_page_and_usage_only(message, expected):
    assert buttons.match_press("backboardd", "Button", message) == expected


def test_press_matcher_ignores_other_processes_and_categories():
    assert buttons.match_press("rapportd", "Button", LEFT) == (None, True)
    assert buttons.match_press("backboardd", "SendHIDEvent", LEFT) == (None, True)
    assert buttons.match_press("backboardd", None, LEFT) == (None, True)


def test_stream_flags_never_replay_history():
    # PAYLOAD for button lines, DEBUG for touch lines.
    assert buttons.STREAM_FLAGS == 0x24
    assert not buttons.STREAM_FLAGS & buttons.OsActivityStreamFlag.HISTORICAL


def test_stale_lines_are_dropped():
    # now is the host's monotonic clock. device is the TV's wall clock.
    fresh = buttons.Freshness(now=1000.0)
    # Right after a connect nothing counts as a press.
    assert fresh.stale(device=950.0, now=1000.2)
    assert not fresh.stale(device=951.5, now=1001.6)
    # A line that took 3 s longer than the baseline arrives too late.
    assert fresh.stale(device=952.0, now=1005.1)
    # A line from before the newest one seen is a replay.
    assert not fresh.stale(device=960.0, now=1010.1)
    assert fresh.stale(device=957.0, now=1010.1)


def test_a_replayed_backlog_after_a_reconnect_is_dropped():
    first = buttons.Freshness(now=1000.0)
    # On the first connection, a short burst of old lines falls in the grace period.
    assert all(first.stale(device=900.0 + step * 0.3, now=1000.0 + step * 0.01) for step in range(60))
    assert not first.stale(device=1003.0, now=1003.4)
    assert first.stale(device=990.0, now=1003.5)
    # A reconnect keeps the lag baseline. A long replay is then late from its first line to its last.
    again = buttons.Freshness(now=2000.0, lag=first.lag)
    assert all(again.stale(device=1900.0 + step * 0.3, now=2000.0 + step * 0.02) for step in range(300))
    assert not again.stale(device=2010.0, now=2010.4)


@pytest.mark.parametrize("step", [5.0, -5.0, 3600.0, -3600.0])
def test_presses_resume_after_a_clock_step(step):
    """Reviewer case: after a clock step every press was dropped while the state stayed active."""
    fresh = buttons.Freshness(now=1000.0)
    assert not fresh.stale(device=1009.9, now=1010.0)
    # The TV clock steps. One press logs several lines within a few milliseconds.
    first = [fresh.stale(device=1020.0 - 0.1 + offset - step, now=1020.0 + offset) for offset in (0, 0.004, 0.008)]
    later = [fresh.stale(device=at - 0.1 - step, now=at) for at in (1030.0, 1100.0, 5000.0)]
    if step < 0:
        # The TV clock moved forward: the lag only shrinks, which is never stale.
        assert first == [False] * 3 and later == [False] * 3
    else:
        # The first press may be lost. A steady new lag is then accepted for good.
        assert first == [True] * 3 and later == [False] * 3
    # A late line is still dropped after the new baseline.
    assert fresh.stale(device=5001.0 - step - 4.0, now=5001.0)


# --- Observer watch --------------------------------------------------------


def entry(message, device=None, process="backboardd", category="Button"):
    return SimpleNamespace(
        timestamp=datetime.fromtimestamp(time.time() if device is None else device),
        filename="/usr/libexec/" + process,
        label=SimpleNamespace(category=category, subsystem="com.apple.BackBoard") if category else None,
        message=message,
    )


def start_activity_answer(status, fmt=plistlib.FMT_BINARY):
    """The three byte strings that the device sends in answer to StartActivity."""
    body = plistlib.dumps({"Status": status}, fmt=fmt)
    count = (len(body).bit_length() + 7) // 8
    return struct.pack("<I", count), len(body).to_bytes(count, "little"), body


class WireConnection:
    """Feeds the real OsTraceService.syslog() from a byte string, then stays silent."""

    def __init__(self, data):
        self.data, self.position = data, 0

    async def send_plist(self, *_, **__):
        pass

    async def recvall(self, size):
        if self.position + size > len(self.data):
            await asyncio.Event().wait()
        self.position += size
        return self.data[self.position - size:self.position]

    async def close(self):
        pass


@pytest.mark.parametrize("status,fmt,expected", [
    ("RequestSuccessful", plistlib.FMT_BINARY, True),
    ("RequestSuccessful", plistlib.FMT_XML, True),
    # A refusal with a short body has a 1-byte length. That read must not count as accepted.
    ("RequestFailed", plistlib.FMT_BINARY, False),
    ("RequestFailed", plistlib.FMT_XML, False),
])
async def test_stream_counts_as_established_only_after_an_accepting_status(status, fmt, expected):
    """Runs the real pymobiledevice3 syslog() read sequence (reviewer repro established.py)."""
    answer = b"".join(start_activity_answer(status, fmt))
    connection = WireConnection(answer)
    service = buttons.OsTraceService(lockdown=None, service=connection)
    established = asyncio.Event()
    seen = []
    buttons.mark_established(service.service, established)
    recvall = connection.recvall

    async def spy(size):
        data = await recvall(size)
        seen.append((connection.position, established.is_set()))
        return data

    connection.recvall = spy
    first = asyncio.create_task(service.syslog(pid=1, stream_flags=4).__anext__())
    await asyncio.sleep(0.05)
    # Never set before the whole status plist was read.
    assert all(not was_set for position, was_set in seen if position < len(answer))
    assert established.is_set() is expected
    assert first.done() is not expected  # An accepted idle stream waits. A refused one raises.
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)


class FakeTrace:
    """Stand-in for OsTraceService. Each instance is one connection."""

    RSD_SERVICE_NAME = buttons.OsTraceService.RSD_SERVICE_NAME
    pids = {"68": {"ProcessName": "backboardd"}, "70": {"ProcessName": "rapportd"}}
    streams = []
    pid_error = None

    accept = True
    status = "RequestSuccessful"

    def __init__(self, lockdown):
        self.rsd = lockdown
        # The answer to StartActivity, then an endless run of 1-byte record markers.
        self.service = SimpleNamespace(recvall=AsyncMock(side_effect=[*start_activity_answer(self.status), *[b"\x02"] * 999]))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def get_pid_list(self):
        if FakeTrace.pid_error:
            raise FakeTrace.pid_error
        return {"Payload": dict(FakeTrace.pids)}

    async def syslog(self, pid, stream_flags):
        lines = asyncio.Queue()
        FakeTrace.streams.append(SimpleNamespace(pid=pid, flags=stream_flags, lines=lines))
        if not FakeTrace.accept:
            await asyncio.Event().wait()  # The device never answers StartActivity.
        # The same reads as pymobiledevice3 11.61 syslog(): count, length, status plist.
        (count,) = struct.unpack("<I", await self.service.recvall(4))
        length = int.from_bytes(await self.service.recvall(count), "little")
        if plistlib.loads(await self.service.recvall(length)).get("Status") != "RequestSuccessful":
            await asyncio.sleep(0.03)  # The refusal reaches the caller a moment later.
            raise ConnectionError("The device refused to start the log stream")
        while True:
            await self.service.recvall(1)
            item = await lines.get()
            if isinstance(item, Exception):
                raise item
            yield item


@pytest.fixture
def watch(monkeypatch, tmp_path):
    FakeTrace.streams, FakeTrace.pid_error, FakeTrace.accept = [], None, True
    FakeTrace.status = "RequestSuccessful"
    FakeTrace.pids = {"68": {"ProcessName": "backboardd"}, "70": {"ProcessName": "rapportd"}}
    monkeypatch.setattr(buttons, "OsTraceService", FakeTrace)
    monkeypatch.setattr(buttons, "CONNECT_GRACE_S", 0)
    monkeypatch.setattr(buttons, "RETRY_MIN_S", 0.01)
    record = tmp_path / "remote.plist"
    record.touch()
    rsd = SimpleNamespace(peer_info={"Services": {FakeTrace.RSD_SERVICE_NAME: {}}})
    tunnel = SimpleNamespace(rsd=rsd)
    observer = SimpleNamespace(
        tunnel=tunnel, record=record, busy=asyncio.Lock(), close_channels=AsyncMock(), pairing=False,
        open_tunnel=AsyncMock(return_value=rsd),
    )
    observer.pairing_active = lambda: observer.pairing
    events = []
    return buttons.ButtonWatch(observer, events.append), observer, events


async def test_watch_reports_one_key_per_press_with_a_pid_filter(watch):
    watch, observer, events = watch
    watch.start()
    try:
        await until(lambda: {"event": "buttons", "state": "active"} in events)
        stream = FakeTrace.streams[0]
        assert (stream.pid, stream.flags) == (68, 0x24)
        up = LEFT.replace("downEvent:1", "downEvent:0")
        for line in (entry(LEFT), entry(up), entry(" 0x1/0x8B/1 began: x"), entry(" 0x1/0x8B/1 finished: x"),
                     entry(LEFT, process="rapportd"), entry("Unknown page:0xC usage:0xCD downEvent:1 x"),
                     entry(LEFT, device=time.time() - 30)):
            stream.lines.put_nowait(line)
        await until(lambda: any(e["event"] == "button_unmatched" for e in events))
        await asyncio.sleep(0.02)
        presses = [e for e in events if e["event"] == "button"]
        assert [e["key"] for e in presses] == ["left"]
        assert presses[0]["received"] >= presses[0]["device_time"]
        assert [e["message"] for e in events if e["event"] == "button_unmatched"] == ["Unknown page:0xC usage:0xCD downEvent:1 x"]
    finally:
        await watch.stop()



def test_touch_and_button_lines_have_their_own_unmatched_budget(watch):
    """Review finding: unknown touch lines used up the budget that unknown button lines needed."""
    watch, observer, events = watch
    for number in range(buttons.UNMATCHED_LIMIT + 2):
        watch._unmatched(f"touch {number}", 100.0, "touch_unmatched")
    watch._unmatched("button", 100.0)
    assert [event["event"] for event in events].count("touch_unmatched") == buttons.UNMATCHED_LIMIT
    assert [event["message"] for event in events if event["event"] == "button_unmatched"] == ["button"]

async def test_watch_restarts_after_a_channel_reset_and_reads_the_pid_again(watch):
    watch, observer, events = watch
    watch.start()
    try:
        await until(lambda: len(FakeTrace.streams) == 1 and events)
        FakeTrace.pids = {"91": {"ProcessName": "backboardd"}}
        FakeTrace.streams[0].lines.put_nowait(ConnectionResetError())
        await until(lambda: len(FakeTrace.streams) == 2)
        assert FakeTrace.streams[1].pid == 91
        await until(lambda: [e["state"] for e in events if e["event"] == "buttons"] == ["active", "unavailable", "active"])
        assert events[1]["reason"] == "stream_failed"
        FakeTrace.streams[1].lines.put_nowait(entry(LEFT))
        await until(lambda: events[-1].get("key") == "left")
        # The watch never closes a tunnel that requests may be using.
        observer.close_channels.assert_not_awaited()
    finally:
        await watch.stop()


async def test_watch_probes_liveness_without_log_traffic(watch, monkeypatch):
    watch, observer, events = watch
    monkeypatch.setattr(buttons, "PROBE_INTERVAL_S", 0.02)
    monkeypatch.setattr(buttons, "PROBE_RETRY_S", 0.02)
    watch.start()
    try:
        await until(lambda: events == [{"event": "buttons", "state": "active"}])
        await asyncio.sleep(0.1)  # Silence is normal. Probes pass and nothing changes.
        assert len(events) == 1 and len(FakeTrace.streams) == 1
        FakeTrace.pid_error = TimeoutError()
        async with observer.busy:  # A request in flight delays the tunnel reset.
            await asyncio.sleep(0.1)
            observer.close_channels.assert_not_awaited()
        await until(lambda: observer.close_channels.await_count == 1)
        await until(lambda: events[-1].get("state") == "unavailable")
    finally:
        await watch.stop()


class ScreenPairingInvalid(Exception):
    pass


async def hang():
    await asyncio.sleep(5)




@pytest.mark.parametrize("case,reason", [
    ("unpaired", "screen_not_paired"), ("pairing", "pairing_in_progress"), ("no_service", "service_missing"),
    ("no_process", "process_not_found"), ("unreachable", "tv_unreachable"), ("slow", "tv_unreachable"),
    ("rejected", "screen_pairing_invalid"), ("refused", "stream_failed"), ("silent", "stream_failed"),
])
async def test_watch_reports_why_it_cannot_run(watch, monkeypatch, case, reason):
    watch, observer, events = watch
    if case == "unpaired":
        observer.record.unlink()
    elif case == "pairing":
        observer.pairing = True
    elif case == "no_service":
        observer.open_tunnel.return_value.peer_info["Services"].clear()
    elif case == "no_process":
        FakeTrace.pids = {}
    elif case == "unreachable":
        observer.open_tunnel.side_effect = ConnectionError("no route")
    elif case == "slow":
        monkeypatch.setattr(buttons, "OPEN_TIMEOUT_S", 0.02)
        observer.open_tunnel.side_effect = hang
    elif case == "rejected":
        observer.open_tunnel.side_effect = ScreenPairingInvalid()
    elif case == "refused":
        # The device answers StartActivity with a failure status. "active" must never show.
        monkeypatch.setattr(FakeTrace, "status", "RequestFailed")
    else:
        # The device never answers the stream request. The watch must not report "active".
        FakeTrace.accept = False
        monkeypatch.setattr(buttons, "ESTABLISH_TIMEOUT_S", 0.05)
    watch.start()
    try:
        await until(lambda: events)
        await asyncio.sleep(0.05)
        assert events == [{"event": "buttons", "state": "unavailable", "reason": reason}]
        if case in ("unpaired", "pairing"):
            observer.open_tunnel.assert_not_awaited()
    finally:
        await watch.stop()


async def test_observer_requests_work_while_the_watch_fails(monkeypatch, tmp_path, capsys):
    for key, value in {"ATV_BRIDGE_HOST": "192.0.2.1", "ATV_CONTROLLER_ID": "test", "ATV_BRIDGE_DATA": str(tmp_path)}.items():
        monkeypatch.setenv(key, value)

    class Tunnel:
        def __init__(self, provider_factory):
            self.rsd = None

        async def aopen(self):
            raise ConnectionError("no route")

        async def aclose(self):
            pass

    monkeypatch.setattr(observer_module, "UserspaceRsdTunnel", Tunnel)
    monkeypatch.setattr(buttons, "RETRY_MIN_S", 0.01)
    observer = observer_module.Observer()
    observer.record.touch()
    try:
        started = time.monotonic()
        assert await observer.call({"method": "buttons_start"}) == {}
        assert await observer.call({"method": "buttons_start"}) == {}  # Idempotent.
        assert time.monotonic() - started < 0.5
        await until(lambda: "unavailable" in capsys.readouterr().out)
        assert await observer.call({"method": "health"}) == {"paired": True, "tunnel": False}
        task = observer.buttons.task
        assert await observer.call({"method": "buttons_stop"}) == {}
        assert task.cancelled() and observer.buttons.task is None
    finally:
        await observer.close()


# --- Touch surface -----------------------------------------------------------


def touch_line(kind, x, y, path=1):
    return f"touchpad Digitizer token: 0xB5890338; {kind}; gen: 7; subevents: [path: {path}; {{{x}, {y}}}; {kind}]; {kind}"


def stroke(start, end, seconds, at=0.0, path=1):
    """Log lines of one contact, every 15 ms, as (device time, message)."""
    count = max(2, round(seconds / 0.015))
    points = [(start[0] + (end[0] - start[0]) * i / count, start[1] + (end[1] - start[1]) * i / count) for i in range(count + 1)]
    lines = [(at, touch_line("down", *points[0], path))]
    lines += [(at + 0.015 * i, touch_line("move", *point, path)) for i, point in enumerate(points[1:], 1)]
    return lines + [(at + 0.015 * count + 0.001, touch_line("up", *points[-1], path))]


def recognize(lines, recognizer=None):
    recognizer = recognizer or touch.TouchRecognizer()
    keys, momentum = [], None
    for at, message in lines:
        gesture = recognizer.touch(message, at)
        keys += gesture.keys
        momentum = gesture.momentum or momentum
    return keys, momentum


def test_real_touch_remote_capture_replays_to_the_expected_keys():
    """Lines from a first-generation Siri Remote on tvOS 26.6.1: five clicks, four swipes, two slow drags, Menu.

    The four swipes are ordinary brisk swipes. They lift off at 9.8, 6.6, 5.2 and 9.4 pad units per second.
    Hardware finding: a swipe like these moved one item, and people expect it to carry focus further.
    Momentum now starts at 4.5 pad units per second, so each of them earns one or two momentum arrows.
    Before, it started at 12 and none of them earned any. The slow drags lift off below 1 and give only their steps.
    """
    recognizer, out, reports = touch.TouchRecognizer(), [], []
    for row in map(json.loads, (FIXTURES / "touch_remote.jsonl").read_text().splitlines()):
        if row["category"] == "Button":
            key, _ = buttons.match_press("backboardd", "Button", row["message"])
            if "downEvent:1" in row["message"]:
                recognizer.button(row["t"])
            if key:
                out.append(key)
        else:
            gesture = recognizer.touch(row["message"], row["t"])
            assert not gesture.unmatched
            out += gesture.keys
            if gesture.momentum:
                out += [f"{gesture.momentum[0]} (momentum)"] * len(gesture.momentum[1])
            reports += gesture.reports
    assert out == (["select"] * 5
                   + ["left"] + ["left (momentum)"] * 2
                   + ["right", "right", "right (momentum)"]
                   + ["up", "up (momentum)"]
                   + ["down"] + ["down (momentum)"] * 2
                   + ["right"] * 4 + ["menu"])
    # One report per contact that ended: four clicks with the finger on the surface, four swipes, two drags.
    assert [report["no_momentum"] for report in reports] == ["clicked"] * 4 + [None] * 4 + ["below_v_min", "no_dominant_axis"]
    assert [report["lift_v"] and max(map(abs, report["lift_v"])) for report in reports[4:]] == [9.82, 6.57, 5.19, 9.36, 0.52, 0.69]
    assert [max(report["peak_v"]) for report in reports[4:]] == [9.82, 8.36, 8.26, 9.36, 0.99, 2.53]
    assert reports[4] == {
        "token": "0xB5890338", "path": 1, "samples": 10, "interval": 0.015,
        "start": [0.9878, 0.6444], "end": [0.1951, 0.6522], "min": [0.1951, 0.5887], "max": [0.9878, 0.6522],
        "travel": [-0.7927, 0.0078], "duration": 0.1964, "rest": 0.017, "span": 1.0,
        "peak_v": [9.82, 0.82], "lift_v": [-9.82, 0.82], "steps": 1, "keys": ["left"], "momentum": 2, "no_momentum": None,
    }


def test_slow_drag_moves_one_item_per_step():
    keys, momentum = recognize(stroke((0.05, 0.5), (0.95, 0.5), 3.0))
    assert keys == ["right", "right"] and momentum is None
    keys, _ = recognize(stroke((0.5, 0.95), (0.5, 0.05), 3.0))
    assert keys == ["up", "up"]
    # A reversal measures the next step from the turning point.
    there_and_back = stroke((0.1, 0.5), (0.4, 0.5), 1.0)[:-1] + stroke((0.4, 0.5), (0.0, 0.5), 1.0, at=1.0)[1:]
    assert recognize(there_and_back)[0] == ["left"]
    # A diagonal has no dominant axis. A tap or a small move does nothing.
    assert recognize(stroke((0.1, 0.1), (0.9, 0.9), 1.0)) == ([], None)
    assert recognize(stroke((0.02, 0.5), (0.03, 0.5), 0.1)) == ([], None)
    assert recognize(stroke((0.5, 0.5), (0.7, 0.5), 1.0)) == ([], None)


def test_moderate_swipe_gives_one_arrow_without_momentum():
    # About 0.75 of the pad at 3.8 pad units per second. Momentum starts at 4.5.
    for start, end, key in [((0.9, 0.6), (0.15, 0.6), "left"), ((0.1, 0.6), (0.85, 0.6), "right"),
                            ((0.5, 0.9), (0.5, 0.15), "up"), ((0.5, 0.1), (0.5, 0.85), "down")]:
        assert recognize(stroke(start, end, 0.2)) == ([key], None)


def test_brisk_swipe_adds_one_or_two_momentum_arrows():
    # Like the captured swipes, which lift off at 5 to 10 pad units per second.
    assert recognize(stroke((0.1, 0.6), (0.85, 0.6), 0.12)) == (["right"], ("right", touch.momentum_delays(1)))  # 6.3
    assert recognize(stroke((0.9, 0.6), (0.15, 0.6), 0.075)) == (["left"], ("left", touch.momentum_delays(2)))  # 10
    assert recognize(stroke((0.5, 0.9), (0.5, 0.15), 0.09)) == (["up"], ("up", touch.momentum_delays(1)))  # 8.3


def test_hard_flick_adds_three_or_four_momentum_arrows():
    # 0.84 of the pad in 60 ms is 14 pad units per second.
    assert recognize(stroke((0.08, 0.5), (0.92, 0.5), 0.06)) == (["right", "right"], ("right", touch.momentum_delays(3)))
    # 0.94 of the pad in 45 ms is about 21 pad units per second.
    keys, momentum = recognize(stroke((0.03, 0.5), (0.97, 0.5), 0.045))
    assert keys == ["right"]
    assert momentum == ("right", touch.momentum_delays(4)) and touch.momentum_count(21) == touch.MOMENTUM_MAX_EXTRA
    # The gaps grow from 40 ms to 120 ms.
    delays = touch.momentum_delays(touch.MOMENTUM_MAX_EXTRA)
    gaps = [round(b - a, 3) for a, b in zip([0, *delays], delays)]
    assert gaps[0] == 0.04 and gaps[-1] == 0.12 and gaps == sorted(gaps)
    assert [touch.momentum_count(speed) for speed in (0, 4.4, 4.5, 8.4, 8.5, 12.5, 16.5, 100)] == [0, 0, 1, 1, 2, 3, 4, 4]
    # A short fast flick is too short for a step. It still moves one item, and its speed carries it on.
    assert recognize(stroke((0.5, 0.5), (0.8, 0.5), 0.03)) == (["right"], ("right", touch.momentum_delays(2)))
    # A sloppy tap slides a little, fast. It covers too little of the pad to be a flick.
    tap = [(0.0, touch_line("down", 0.5, 0.5)), (0.008, touch_line("move", 0.57, 0.5)),
           (0.016, touch_line("move", 0.64, 0.5)), (0.017, touch_line("up", 0.64, 0.5))]
    recognizer = touch.TouchRecognizer()
    reports = [report for at, line in tap for report in recognizer.touch(line, at).reports]
    assert recognize(tap) == ([], None)
    assert [(report["no_momentum"], report["lift_v"]) for report in reports] == [("too_short", [8.75, 0.0])]


def test_a_finger_that_rests_before_lifting_does_not_flick():
    """Reviewer case: a fast slide, a rest with no log lines, then a lift fired momentum arrows."""
    flick = stroke((0.03, 0.5), (0.97, 0.5), 0.045)
    moves, lift = flick[:-1], flick[-1][1]
    assert recognize(moves + [(moves[-1][0] + 0.03, lift)])[1] == ("right", touch.momentum_delays(4))
    for rest in (0.06, 0.341, 2.0):
        assert recognize(moves + [(moves[-1][0] + rest, lift)]) == (["right"], None)
    # Repeated lines at the same position during the rest do not count as movement.
    still = [(moves[-1][0] + 0.015 * i, touch_line("move", 0.97, 0.5)) for i in range(1, 30)]
    assert recognize(moves + still + [(still[-1][0] + 0.001, lift)]) == (["right"], None)
    # A short flick after a rest at the start still counts.
    late = [(0.0, touch_line("down", 0.5, 0.5))] + [(at + 1.0, m) for at, m in stroke((0.5, 0.5), (0.8, 0.5), 0.03)[1:]]
    assert recognize(late) == (["right"], ("right", touch.momentum_delays(2)))


def test_a_surface_that_reports_slowly_can_still_flick():
    """The lift-off limits were set for a line every 15 ms. Another remote may log fewer lines."""
    def contact(points, interval, rest):
        lines = [(interval * i, touch_line("move" if i else "down", x, 0.5)) for i, x in enumerate(points)]
        return lines + [(lines[-1][0] + rest, touch_line("up", points[-1], 0.5))]

    # A line every 40 ms, at 5 pad units per second. A lift 60 ms after the last line is no rest there.
    points = [0.1, 0.3, 0.5, 0.7, 0.9]
    assert recognize(contact(points, 0.04, 0.06)) == (["right", "right"], ("right", touch.momentum_delays(1)))
    assert recognize(contact(points, 0.04, 0.13)) == (["right", "right"], None)
    # At 15 ms per line the same 60 ms are a rest, as before.
    assert recognize(contact(points, 0.015, 0.06)) == (["right", "right"], None)
    # A line every 80 ms: the lift-off window holds one sample, so the last two give the speed.
    assert recognize(contact([0.08, 0.5, 0.92], 0.08, 0.01)) == (["right", "right"], ("right", touch.momentum_delays(1)))


def test_distances_are_fractions_of_a_fixed_pad():
    """Review finding: the range that the finger had used counted as the pad.

    Brisk flicks in the middle of a 0-to-1 pad shrank it to half, so every later swipe gave twice the
    arrows, and a reset kept that. The pad is now PAD_SPAN, whatever the finger did.
    """
    recognizer = touch.TouchRecognizer()
    for number in range(8):
        assert recognize(stroke((0.4, 0.5), (0.65, 0.5), 0.04, at=number * 2.0), recognizer)[0] == ["right"]
    drag = stroke((0.3, 0.55), (0.75, 0.55), 1.0, at=20.0)
    assert recognize(drag, recognizer) == recognize(drag) == (["right"], None)
    reports = [report for at, line in drag for report in recognizer.touch(line, at).reports]
    assert [(report["span"], report["steps"]) for report in reports] == [(touch.PAD_SPAN, 1)]


def test_a_second_finger_is_ignored():
    recognizer = touch.TouchRecognizer()
    first, second = stroke((0.03, 0.5), (0.97, 0.5), 0.045), stroke((0.03, 0.6), (0.97, 0.6), 0.045, path=2)
    lines = [line for pair in zip(first, second) for line in pair]
    keys, momenta, downs = [], [], 0
    for at, message in lines:
        gesture = recognizer.touch(message, at)
        keys += gesture.keys
        downs += gesture.touch_down
        momenta += [gesture.momentum] if gesture.momentum else []
    # One finger's steps and one momentum plan. Each touch-down still cancels older momentum.
    assert keys == ["right"] and momenta == [("right", touch.momentum_delays(4))] and downs == 2
    assert recognizer.contacts == {}
    # A lost lift-off line must not block later touches: a silent contact gives way.
    recognize(stroke((0.1, 0.5), (0.2, 0.5), 0.05, at=0.5)[:-1], recognizer)
    assert recognize(stroke((0.9, 0.5), (0.1, 0.5), 0.2, at=0.6, path=2), recognizer) == ([], None)
    assert recognize(stroke((0.9, 0.5), (0.1, 0.5), 0.2, at=3.0, path=2), recognizer) == (["left"], None)
    # The second finger can become the tracked one after the first has lifted.
    assert recognize(stroke((0.9, 0.5), (0.1, 0.5), 0.2, at=5.0, path=2), recognizer) == (["left"], None)


def test_click_during_a_contact_cancels_its_swipe():
    recognizer = touch.TouchRecognizer()
    lines = stroke((0.05, 0.5), (0.95, 0.5), 0.045)
    assert recognize(lines[:2], recognizer) == ([], None)
    recognizer.button(lines[1][0])  # Select, or a ring click on a clickpad remote.
    assert recognize(lines[2:], recognizer) == ([], None)
    # A contact that starts right after the click also belongs to it. A later one does not.
    assert recognize(stroke((0.05, 0.5), (0.95, 0.5), 0.2, at=0.1), recognizer) == ([], None)
    assert recognize(stroke((0.05, 0.5), (0.95, 0.5), 0.3, at=2.0), recognizer) == (["right", "right"], None)


async def test_watch_sends_swipes_and_momentum_and_a_new_touch_cancels_it(watch, monkeypatch):
    watch, observer, events = watch
    monkeypatch.setattr(touch, "MOMENTUM_FIRST_GAP_S", 0.05)
    monkeypatch.setattr(touch, "MOMENTUM_LAST_GAP_S", 0.05)

    def keys():
        return [(e["key"], e["gesture"]) for e in events if e["event"] == "button"]

    def feed(lines, base):
        for at, message in lines:
            FakeTrace.streams[0].lines.put_nowait(entry(message, device=base + at, category="TouchEvents"))

    watch.start()
    try:
        await until(lambda: {"event": "buttons", "state": "active"} in events)
        base = time.time()
        flick = stroke((0.03, 0.5), (0.97, 0.5), 0.045)
        feed(flick, base)
        await until(lambda: len(keys()) == 5)
        assert keys() == [("right", "swipe")] + [("right", "momentum")] * 4
        assert not any(e["event"].endswith("unmatched") for e in events)
        # Each contact that ends gives one report, for tuning from the app log.
        (report,) = [e for e in events if e["event"] == "gesture"]
        assert (report["keys"], report["momentum"], report["no_momentum"], report["samples"]) == (["right"], 4, None, 4)
        assert report["lift_v"] == [20.89, 0.0] and report["start"] == [0.03, 0.5] and report["end"] == [0.97, 0.5]
        # A second flick, then a new touch-down before its momentum arrows go out.
        del events[:]
        feed(flick, base + 1)
        feed([(0.0, touch_line("down", 0.5, 0.5, path=2))], base + 1.06)
        await asyncio.sleep(0.3)
        assert keys() == [("right", "swipe")]
        # A new momentum plan replaces a running one. The old arrows stop.
        del events[:]
        watch.momentum = asyncio.create_task(watch._glide("left", [0.05, 0.1]))
        old = watch.momentum
        feed(flick, base + 2)
        await until(lambda: len(keys()) == 5)
        await asyncio.sleep(0.15)
        assert old.cancelled() and keys() == [("right", "swipe")] + [("right", "momentum")] * 4
        # A click during a contact: Select goes through, the swipe does not.
        del events[:]
        feed(flick[:2], base + 3)
        FakeTrace.streams[0].lines.put_nowait(entry(
            "Select page:0xC usage:0x80 downEvent:1 source:verifiedExternalAccessory senderID:1 down", device=base + 3.02))
        feed(flick[2:], base + 3)
        await until(lambda: [e["key"] for e in events if e["event"] == "button"] == ["select"])
        await asyncio.sleep(0.3)
        assert [e["key"] for e in events if e["event"] == "button"] == ["select"]
        assert [e["no_momentum"] for e in events if e["event"] == "gesture"] == ["clicked"]
        # Stale touch lines move nothing.
        del events[:]
        feed(flick, base - 60)
        await asyncio.sleep(0.2)
        assert events == []
    finally:
        await watch.stop()


ATTACHED = ("digitizer attached (sender 100004008) service:<BKIOHIDService: 0xd787c6a20; serviceStatus: matched; IOHIDService: "
            "IOHIDService name:AppleEmbeddedBluetoothTouch id:0x100004008 primaryUsagePage:0xd primaryUsage:0x1 "
            "transport:BluetoothLowEnergy locationID:-1735663521 reportInterval:8000; senderID: 0x100004008>")


async def test_watch_logs_what_a_remote_sends_for_touch(watch, caplog):
    """The clickpad remote's touch lines were never captured. The log must show them, whatever their shape."""
    watch, observer, events = watch
    caplog.set_level("INFO")

    def feed(message, category="TouchEvents"):
        FakeTrace.streams[-1].lines.put_nowait(entry(message, category=category))

    def logged(text):
        return [record.getMessage() for record in caplog.records if text in record.getMessage()]

    watch.start()
    try:
        await until(lambda: {"event": "buttons", "state": "active"} in events)
        # A line shape that the matcher does not know.
        unknown = "pad contact 3 began x:120 y:88 down"
        for message in ("touches:1 ranges:1 then:NO now:YES", unknown, unknown, ATTACHED):
            feed(message)
        feed("destinations for Digitizer event: (<keyboardFocus; token: 0xB5890338>)", "EventDelivery")
        feed("destinations for Digitizer event: (<keyboardFocus; token: 0xB5890338>)", "EventDelivery")
        feed("new dispatching rulesets for pid:37: [\n    (all senders); desc: (Scroll, Digitizer) -> <keyboardFocus>\n]", "Dispatch")
        for at, message in stroke((0.03, 0.5), (0.97, 0.5), 0.045):
            FakeTrace.streams[-1].lines.put_nowait(entry(message, device=time.time() + at, category="TouchEvents"))
        await until(lambda: any(e["event"] == "gesture" for e in events))
        # The first raw line, and the first one with "down" in it, once each. Neither needs the matcher.
        assert logged("First touch line of this watch") == ["First touch line of this watch: touches:1 ranges:1 then:NO now:YES"]
        assert logged("First touch-down line of this watch") == [f"First touch-down line of this watch: {unknown}"]
        # A touch line in another log category is shown once per category.
        assert len(logged("First line about touch in log category EventDelivery")) == 1
        # A line with line breaks stays one log line. The app reads the level from the start of each line.
        assert logged("log category Dispatch") == [
            "First line about touch in log category Dispatch: new dispatching rulesets for pid:37: [ (all senders); "
            "desc: (Scroll, Digitizer) -> <keyboardFocus> ]"]
        # Lines of an unknown shape are reported. Known lines without a position are not.
        assert [e["message"] for e in events if e["event"] == "touch_unmatched"] == [unknown, unknown]
        # The report names the device that announced itself.
        (report,) = [e for e in events if e["event"] == "gesture"]
        assert report["devices"] == ["AppleEmbeddedBluetoothTouch 0x100004008 BluetoothLowEnergy"]
        # A new watch logs its first lines again.
        await watch.stop()
        watch.start()
        await until(lambda: len(FakeTrace.streams) == 2)
        feed(unknown)
        await until(lambda: len(logged("First touch-down line of this watch")) == 2)
    finally:
        await watch.stop()


# --- Tunnel sharing with the real pymobiledevice3 lifecycle ------------------


@pytest.fixture
def real_tunnel(monkeypatch, tmp_path):
    """An Observer on the real UserspaceRsdTunnel. Only the network parts are replaced."""
    import contextlib

    import pymobiledevice3.remote.userspace_tunnel as userspace
    from pymobiledevice3.services.accessibilityaudit import AccessibilityAudit
    from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider

    for key, value in {"ATV_BRIDGE_HOST": "192.0.2.1", "ATV_CONTROLLER_ID": "test", "ATV_BRIDGE_DATA": str(tmp_path)}.items():
        monkeypatch.setenv(key, value)
    link = SimpleNamespace(handshake=0.2, connects=0, hang=False)

    class Service:
        async def close(self):
            pass

        @contextlib.asynccontextmanager
        async def start_tcp_tunnel(self):
            link.connects += 1
            await asyncio.sleep(3600 if link.hang else link.handshake)
            tun = SimpleNamespace(set_peer=lambda address: None)
            yield SimpleNamespace(client=SimpleNamespace(tun=tun), address="fd00::1", port=1, auxiliary_metadata={})

    class Dial:
        def __init__(self, *_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def dial(self, *_):
            pass

    class Rsd:
        peer_info = {"Services": {name: {} for name in (
            FakeTrace.RSD_SERVICE_NAME, DvtProvider.RSD_SERVICE_NAME, AccessibilityAudit.RSD_SERVICE_NAME)}}

        def __init__(self, *_, **__):
            pass

        async def connect(self):
            pass

        async def close(self):
            pass

    async def never(self, client):
        await asyncio.Event().wait()

    async def provider(self):
        return SimpleNamespace(service=Service(), lockdown=None)

    monkeypatch.setattr(userspace, "UserspaceDialPlane", Dial)
    monkeypatch.setattr(userspace, "RemoteServiceDiscoveryService", Rsd)
    monkeypatch.setattr(userspace.UserspaceRsdTunnel, "_watch_transport_closed", never)
    monkeypatch.setattr(observer_module.Observer, "provider", provider)
    FakeTrace.streams, FakeTrace.pid_error, FakeTrace.accept, FakeTrace.status = [], None, True, "RequestSuccessful"
    FakeTrace.pids = {"68": {"ProcessName": "backboardd"}}
    monkeypatch.setattr(buttons, "OsTraceService", FakeTrace)
    monkeypatch.setattr(buttons, "RETRY_MIN_S", 0.01)
    events = []
    monkeypatch.setattr(observer_module, "emit", events.append)
    observer = observer_module.Observer()
    observer.record.touch()
    return observer, link, events


async def test_two_plain_opens_collide_in_pymobiledevice3(real_tunnel):
    """The defect that open_tunnel() prevents: a second aopen() during the first one raises."""
    observer, link, _ = real_tunnel
    try:
        first = asyncio.create_task(observer.tunnel.aopen())
        await asyncio.sleep(0.05)
        with pytest.raises(Exception, match="already active"):
            await observer.tunnel.aopen()
        await first
    finally:
        await observer.close()


async def test_watch_and_requests_share_one_tunnel_open(real_tunnel):
    observer, link, events = real_tunnel
    try:
        # The watch starts the connect. Requests arrive while it runs.
        assert await observer.call({"method": "buttons_start"}) == {}
        await asyncio.sleep(0.05)
        assert link.connects == 1 and observer.tunnel.rsd is None
        started = time.monotonic()
        results = await asyncio.gather(
            observer.call({"method": "health", "verify": True}),
            observer.open_tunnel(),
            observer.open_tunnel(),
        )
        # They joined the running connect. Nobody failed and nobody connected again.
        assert results[0] == {"paired": True, "tunnel": True} and results[1] is results[2] is observer.tunnel.rsd
        assert link.connects == 1 and time.monotonic() - started < link.handshake + 0.2
        await until(lambda: {"event": "buttons", "state": "active"} in events)
        # After a channel reset both sides reopen through the same path.
        await observer.close_channels()
        assert observer.tunnel.rsd is None
        FakeTrace.streams[0].lines.put_nowait(ConnectionResetError())  # A closed tunnel ends the stream.
        await asyncio.sleep(0.03)  # The watch is reconnecting now.
        assert await observer.call({"method": "health", "verify": True}) == {"paired": True, "tunnel": True}
        await until(lambda: len(FakeTrace.streams) == 2 and events[-1] == {"event": "buttons", "state": "active"})
        assert link.connects == 2
    finally:
        await observer.close()
    assert observer.tunnel.rsd is None


async def test_slow_watch_connect_does_not_hold_requests_or_the_tunnel(real_tunnel, monkeypatch):
    """With an unreachable TV the watch gives up fast and its connect never blocks a close."""
    observer, link, events = real_tunnel
    link.hang = True
    monkeypatch.setattr(buttons, "OPEN_TIMEOUT_S", 0.3)
    try:
        await observer.call({"method": "buttons_start"})
        await asyncio.sleep(0.05)
        # A request fails by its own deadline, not the watch's.
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                await observer.open_tunnel()
        # A close does not wait for the watch's connect.
        await observer.close_channels()
        assert time.monotonic() - started < 0.25
        # The watch survives the cancelled connect, reports it and tries again.
        await until(lambda: {"event": "buttons", "state": "unavailable", "reason": "tv_unreachable"} in events)
        await until(lambda: link.connects >= 2)
        assert not observer.buttons.task.done()
        # When nobody waits any more, the connect is cancelled and the tunnel is free again.
        await observer.buttons.stop()
        await asyncio.sleep(0.01)
        assert observer.opening[0].done()
        link.hang = False
        assert await observer.call({"method": "health", "verify": True}) == {"paired": True, "tunnel": True}
    finally:
        await observer.close()


async def test_request_does_not_join_a_cancelled_connect(monkeypatch, tmp_path):
    """Reviewer repro open_join.py: a request arrives while a cancelled connect still unwinds."""
    for key, value in {"ATV_BRIDGE_HOST": "192.0.2.1", "ATV_CONTROLLER_ID": "test", "ATV_BRIDGE_DATA": str(tmp_path)}.items():
        monkeypatch.setenv(key, value)
    rsd = SimpleNamespace(peer_info={"Services": {}})

    class Tunnel:
        def __init__(self, provider_factory):
            self.rsd, self.opens, self.unwinding = None, 0, False

        async def aopen(self):
            self.opens += 1
            if self.opens > 1:
                assert not self.unwinding  # A new connect must wait for the old one to finish.
                self.rsd = rsd
                return rsd
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                self.unwinding = True
                await asyncio.sleep(0.2)  # Closing a half-open tunnel takes time.
                self.unwinding = False
                raise

        async def aclose(self):
            self.rsd = None

    monkeypatch.setattr(observer_module, "UserspaceRsdTunnel", Tunnel)
    observer = observer_module.Observer()
    observer.record.touch()
    try:
        # The watch is the only waiter and gives up.
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                await observer.open_tunnel()
        assert observer.opening[0].cancelling() and not observer.opening[0].done()
        # Two requests arrive during the unwind. Both get a new connect and no error.
        assert await asyncio.gather(observer.open_tunnel(), observer.open_tunnel()) == [rsd, rsd]
        assert observer.tunnel.opens == 2
    finally:
        await observer.close()


# --- Pipe demux ------------------------------------------------------------

SCRIPT = """import json, sys
print(json.dumps({"event": "buttons", "state": "active"}), flush=True)
for line in sys.stdin:
    method = json.loads(line)["method"]
    if method == "die":
        sys.exit(0)
    print(json.dumps({"event": "button", "key": "left"}), flush=True)
    print(json.dumps({"ok": True, "method": method}), flush=True)
    print(json.dumps({"event": "button", "key": "select"}), flush=True)
"""


async def test_a_stray_output_line_does_not_stall_later_requests(tmp_path):
    script = """import json, sys
for line in sys.stdin:
    method = json.loads(line)["method"]
    if method == "garbled":
        print("not json", flush=True)
        continue
    print(json.dumps({"ok": True, "method": method}), flush=True)
    print("a stray line while idle", flush=True)
    print("[1, 2]", flush=True)
"""
    client = ObserverClient("192.0.2.1", State(tmp_path), [sys.executable, "-c", script])
    try:
        assert (await client.call("first"))["ok"]
        await asyncio.sleep(0.2)
        started = time.monotonic()
        assert (await client.call("second"))["method"] == "second"
        # An unreadable response fails its request at once, not after the 35 s deadline.
        with pytest.raises(Fault) as failure:
            await client.call("garbled")
        assert failure.value.code == "observer_unavailable" and time.monotonic() - started < 5
        assert (await client.call("third"))["method"] == "third"
    finally:
        await client.close()


async def test_client_separates_events_from_responses(tmp_path):
    client = ObserverClient("192.0.2.1", State(tmp_path), [sys.executable, "-c", SCRIPT])
    events = []
    client.subscribe(events.append)
    try:
        assert (await client.call("ax_focused"))["method"] == "ax_focused"
        assert (await client.call("screenshot"))["method"] == "screenshot"
        await until(lambda: len(events) == 5)
        assert [e.get("key") or e.get("state") for e in events] == ["active", "left", "select", "left", "select"]
        # A dead process fails the request at once and reports "closed".
        started = time.monotonic()
        with pytest.raises(Fault) as failure:
            await client.call("die")
        assert failure.value.code == "observer_unavailable" and time.monotonic() - started < 5
        assert events[-1] == {"event": "closed"}
        # The next call starts a new process.
        assert (await client.call("health"))["ok"]
    finally:
        await client.close()
    assert events[-1] == {"event": "closed"}


# --- App-side lifecycle ----------------------------------------------------


class FakeObserver:
    def __init__(self):
        self.calls = []
        self.listeners = []
        self.failure = None
        self.reply = {"event": "buttons", "state": "active"}

    def subscribe(self, listener):
        self.listeners.append(listener)

    def emit(self, event):
        for listener in self.listeners:
            listener(event)

    async def call(self, method, **params):
        self.calls.append(method)
        if method.startswith("buttons"):
            if self.failure:
                raise self.failure
            if method == "buttons_start" and self.reply:
                self.emit(self.reply)
            return {"ok": True}
        return {"ok": True, "focused": {"caption": "TV"}, "items": []}

    def count(self, method):
        return self.calls.count(method)


@pytest.fixture
async def remote(service, monkeypatch):
    observer = FakeObserver()
    service.operations.observer = observer
    sender = SimpleNamespace(mode="mirror", started=True, stop=AsyncMock())
    service.operations.presenter = sender
    yield service, observer, service.tools.remote_input
    await service.tools.remote_input.close()


async def test_watch_follows_the_presented_page(remote):
    service, observer, control = remote
    stage = service.stage
    await stage.show("a", "/c/a/index.html")
    await asyncio.sleep(0.02)
    assert observer.calls == [] and control.status() is None and not control.tasks

    await stage.show("b", "/c/b/index.html", remote="keys")
    await until(lambda: control.state == "active")
    assert observer.calls == ["buttons_start"]
    assert control.status() == {"mode": "keys", "state": "active", "presses": 0, "last": None}
    # In auto mode the mirror can fall back to a recording. Keys cannot reach a recording.
    service.operations.presenter = SimpleNamespace(mode="auto", active=SimpleNamespace(mode="play_url"))
    assert control.status()["reason"] == "not_mirrored"
    service.operations.presenter = SimpleNamespace(mode="mirror", stop=AsyncMock())

    # A second page with the option keeps the same watch.
    await stage.show("c", "/c/c/index.html", remote="focus")
    await asyncio.sleep(0.02)
    assert observer.calls == ["buttons_start"] and control.status()["mode"] == "focus"

    # A page without the option stops it.
    await stage.show("d", "/c/d/index.html")
    await until(lambda: observer.count("buttons_stop") == 1)
    assert control.status() is None

    await stage.show("e", "/c/e/index.html", remote="keys")
    await until(lambda: observer.count("buttons_start") == 2)
    await stage.hide()
    await until(lambda: observer.count("buttons_stop") == 2)
    observer.emit({"event": "button", "key": "left"})
    assert control.presses == 0 and control.queue.empty()


async def test_menu_and_home_are_noted_for_the_mirror_and_never_reach_the_page(remote):
    service, observer, control = remote
    await service.stage.show("a", "/c/a/index.html", remote="tvos")
    await until(lambda: control.state == "active")
    assert control.exit_press() is None
    observer.emit({"event": "button", "key": "menu"})
    assert control.exit_press() == "menu"
    observer.emit({"event": "button", "key": "home"})
    assert control.exit_press() == "home"
    assert control.presses == 0 and control.queue.empty() and control.status()["last"] is None
    # Only a recent press explains a lost mirror.
    control.exit = ("menu", time.monotonic() - remote_input.EXIT_WINDOW_S - 0.1)
    assert control.exit_press() is None
    # A new page starts without one. A screen saver wake before a show sends its own Menu.
    observer.emit({"event": "button", "key": "menu"})
    await service.stage.show("b", "/c/b/index.html", remote="tvos")
    assert control.exit_press() is None


async def test_watch_restarts_when_the_observer_process_closes(remote, monkeypatch):
    service, observer, control = remote
    control.delay = 0.01
    await service.stage.show("a", "/c/a/index.html", remote="keys")
    await until(lambda: control.state == "active")
    observer.reply = None
    observer.emit({"event": "closed"})
    assert control.status()["state"] == "starting"
    await until(lambda: observer.count("buttons_start") == 2)
    observer.emit({"event": "buttons", "state": "active"})
    assert control.status()["state"] == "active"
    # The observer reports its own stream retries.
    observer.emit({"event": "buttons", "state": "unavailable", "reason": "ConnectionResetError"})
    assert control.status()["reason"] == "ConnectionResetError"
    assert observer.count("buttons_start") == 2


async def test_start_failure_retries_and_does_not_break_reads(remote):
    service, observer, control = remote
    control.delay = 0.01
    observer.failure = Fault("pairing_required", "Remote Pairing is required.", "Pair the observer.")
    await service.stage.show("a", "/c/a/index.html", remote="keys")
    await until(lambda: observer.count("buttons_start") >= 2)
    assert control.status() == {"mode": "keys", "state": "unavailable", "reason": "screen_not_paired", "presses": 0, "last": None}
    result = await service.tools.call("tv_look", {})
    assert result["ok"] and result["screen"]["presenting"]["remote"]["state"] == "unavailable"
    observer.failure = None
    await until(lambda: control.state == "active")


async def test_touch_reports_reach_the_app_log_at_info_level(remote, caplog, monkeypatch):
    service, observer, control = remote
    caplog.set_level("INFO", logger="atv_bridge_app.present.remote_input")
    monkeypatch.setattr(remote_input, "TOUCH_LOG_WINDOW_S", 0.2)

    def lines():
        return [record.getMessage() for record in caplog.records if record.getMessage().startswith("Remote touch")]

    report = {"event": "gesture", "samples": 10, "lift_v": [-9.82, 0.82], "steps": 1, "momentum": 2, "no_momentum": None}
    observer_event = observer.emit
    # No page asked for remote input: nothing is logged.
    observer.subscribe(control.on_event)
    control.subscribed = True
    observer_event(report)
    assert lines() == []
    await service.stage.show("a", "/c/a/index.html", remote="tvos")
    await until(lambda: control.state == "active")
    observer_event(report)
    assert lines() == ['Remote touch: {"samples":10,"lift_v":[-9.82,0.82],"steps":1,"momentum":2,"no_momentum":null}']
    assert caplog.records[-1].levelname == "INFO"
    # A finger can touch the remote many times a second. The log keeps a limit per window.
    for _ in range(remote_input.TOUCH_LOG_LIMIT + 5):
        observer_event(report)
    assert len(lines()) == remote_input.TOUCH_LOG_LIMIT
    await asyncio.sleep(0.25)
    observer_event(report)
    assert len(lines()) == remote_input.TOUCH_LOG_LIMIT + 1 and lines()[-1].endswith("(6 earlier ones not logged)")
    # A touch line of an unknown shape is shown too.
    observer_event({"event": "touch_unmatched", "message": "pad contact 3 began"})
    assert "Remote touch line with an unknown shape: pad contact 3 began" in caplog.text


# --- tv_show ---------------------------------------------------------------


def present_as(service, mode):
    """Fake the presentation. Record the remote mode that tv_show passes, and show it as present does."""
    shown = []

    async def present(*, remote, **_):
        shown.append(remote)
        await service.stage.show("page", "/c/page/index.html", **({"remote": remote} if remote != "off" else {}))
        return {"page_id": "page", "mode": mode, "verified": True, "evidence": []}

    service.operations.present = present
    return shown


@pytest.mark.parametrize("source,mode", [
    ({"html": "<h1>x</h1>"}, "tvos"),
    ({"files": {"index.html": "<h1>x</h1>"}}, "tvos"),
    ({"bundle": "b1"}, "tvos"),
    # A live site was not laid out for the tvOS lift, which could clip or cover a neighbor.
    ({"url": "https://example.org/"}, "focus"),
])
async def test_show_picks_the_remote_mode_by_page_type(remote, source, mode):
    service, observer, control = remote
    shown = present_as(service, "mirror")
    result = await service.tools.call("tv_show", source)
    # The result reports the mode in effect, also when the caller did not ask for one.
    assert shown == [mode] and result["ok"] and result["data"]["remote"] == {"mode": mode, "state": "starting"}
    assert result["screen"]["presenting"]["remote"]["mode"] == mode
    await until(lambda: control.state == "active")
    assert observer.count("buttons_start") == 1


async def test_show_reports_remote_input(remote):
    service, observer, control = remote
    shown = present_as(service, "mirror")
    call = service.tools.call
    result = await call("tv_show", {"html": "<h1>x</h1>"})
    # The show does not wait for the watch. Later results report its state.
    assert result["ok"] and result["data"]["remote"] == {"mode": "tvos", "state": "starting"}
    await until(lambda: control.state == "active")
    observer.emit({"event": "button", "key": "select", "received": time.time()})
    screen = (await call("tv_look", {}))["screen"]
    live = screen["presenting"]["remote"]
    assert live["state"] == "active" and live["presses"] == 1 and live["last"]["key"] == "select" and live["last"]["at"]

    # off sends no input. Its result has no remote fields, as before.
    result = await call("tv_show", {"html": "<h1>x</h1>", "remote": "off"})
    assert "remote" not in result["data"] and "remote" not in result["screen"]["presenting"]
    await until(lambda: observer.count("buttons_stop") == 1)

    # An explicit mode wins over the default for the page type.
    result = await call("tv_show", {"html": "<h1>x</h1>", "remote": "focus"})
    assert result["data"]["remote"] == {"mode": "focus", "state": "starting"}
    await until(lambda: control.state == "active")
    # The next pages find the watch running.
    result = await call("tv_show", {"url": "https://example.org/", "remote": "tvos"})
    assert result["data"]["remote"] == {"mode": "tvos", "state": "active"}
    # Earlier callers sent "keys". It is an alias of focus.
    result = await call("tv_show", {"url": "https://example.org/", "remote": "keys"})
    assert result["ok"] and result["data"]["remote"] == {"mode": "focus", "state": "active"}
    assert shown == ["tvos", "off", "focus", "tvos", "focus"]

    # Dismissing takes no remote mode and stops the watch.
    dismissed = await call("tv_show", {})
    assert dismissed["ok"] and dismissed["screen"]["presenting"] is None and "remote" not in dismissed["data"]
    await until(lambda: observer.count("buttons_stop") == 2)
    assert len(shown) == 5 and control.status() is None


@pytest.mark.parametrize("case,reason", [("unpaired", "screen_not_paired"), ("crash", "observer_unavailable"),
                                        ("stream", "service_missing"), ("video", "not_mirrored")])
async def test_show_succeeds_when_remote_input_cannot_start(remote, case, reason):
    service, observer, control = remote
    present_as(service, "play_url" if case == "video" else "mirror")
    if case == "video":
        service.operations.presenter = SimpleNamespace(mode="play_url", stop=AsyncMock())
    if case == "unpaired":
        observer.failure = Fault("pairing_required", "Remote Pairing is required.", "Pair the observer.")
    elif case == "crash":
        observer.failure = Fault("observer_unavailable", "Observer did not respond.", "Check.")
    elif case == "stream":
        observer.reply = {"event": "buttons", "state": "unavailable", "reason": reason}
    result = await service.tools.call("tv_show", {"html": "<h1>x</h1>"})
    assert result["ok"] and result["verified"]
    if case == "video":
        # A recording never gets keys. The show says so at once.
        assert result["data"]["remote"] == {"mode": "tvos", "state": "unavailable", "reason": reason}
        assert observer.calls.count("buttons_start") == 0
    else:
        # The watch starts after the show returns. Later results report why it cannot run.
        assert result["data"]["remote"] == {"mode": "tvos", "state": "starting"}
        await until(lambda: control.state == "unavailable")
    remote = (await service.tools.call("tv_look", {}))["screen"]["presenting"]["remote"]
    assert (remote["state"], remote["reason"]) == ("unavailable", reason)


async def test_video_only_bridge_never_starts_the_watch(remote):
    service, observer, control = remote
    service.operations.presenter = SimpleNamespace(mode="play_url")
    await service.stage.show("a", "/c/a/index.html", remote="keys")
    await asyncio.sleep(0.02)
    assert observer.calls == [] and control.status()["reason"] == "not_mirrored"


@pytest.mark.parametrize("case,reason", [("recording", "not_mirrored"), ("failed", "mirror_failed")])
async def test_watch_stops_when_the_mirror_falls_back_or_fails(remote, caplog, case, reason):
    """After the show, auto mode can fall back to a recording, or the mirror can fail for good."""
    service, observer, control = remote
    mirror = SimpleNamespace(mode="mirror", started=True, status=lambda: {"ok": True})
    presenter = service.operations.presenter = SimpleNamespace(mode="auto", active=mirror, stop=AsyncMock())
    control.interaction.binding = AsyncMock(side_effect=AssertionError("no dispatch without a mirror"))
    await service.stage.show("a", "/c/a/index.html", remote="keys")
    await until(lambda: control.state == "active")
    assert control.wanted()
    if case == "recording":
        presenter.active = SimpleNamespace(mode="play_url")
    else:
        mirror.status = lambda: {"ok": False, "error": {"code": "sender_failed"}}
    # Senders announce every status change.
    service.operations.sender.events.emit({"ok": True})
    await until(lambda: observer.count("buttons_stop") == 1)
    assert control.status() == {"mode": "keys", "state": "unavailable", "reason": reason, "presses": 0, "last": None}
    observer.emit({"event": "button", "key": "left"})
    await asyncio.sleep(0.02)
    assert control.presses == 0 and control.queue.empty() and "not delivered" not in caplog.text
    # The mirror comes back.
    presenter.active = SimpleNamespace(mode="mirror", started=True)
    service.operations.sender.events.emit({"ok": True})
    await until(lambda: observer.count("buttons_start") == 2 and control.status()["state"] == "active")


def test_show_schema_accepts_the_remote_option():
    valid = Draft202012Validator(SCHEMAS["tv_show"]).is_valid
    spec = SCHEMAS["tv_show"]["properties"]["remote"]
    # The default depends on the page type, so the schema states it in words only.
    assert spec["enum"] == ["off", "focus", "tvos"] and "default" not in spec
    assert "tvos for html, files and bundle, and focus for url" in spec["description"]
    assert all(valid({"html": "x", "remote": mode}) for mode in ("off", "focus", "tvos"))
    assert not valid({"html": "x", "remote": "mouse"}) and not valid({"remote": "focus"})
    assert Draft202012Validator(ROUTED_SCHEMAS["tv_show"]).is_valid({"tv": "Bedroom", "html": "x", "remote": "focus"})


def test_keys_is_an_unlisted_alias_of_focus():
    for schemas in (SCHEMAS, ROUTED_SCHEMAS):
        assert not Draft202012Validator(schemas["tv_show"]).is_valid({"html": "x", "remote": "keys"})
    assert canonical("tv_show", {"html": "x", "remote": "keys"}) == {"html": "x", "remote": "focus"}
    # A call with no source stays invalid. Other tools and values are unchanged.
    assert not Draft202012Validator(SCHEMAS["tv_show"]).is_valid(canonical("tv_show", {"remote": "keys"}))
    assert canonical("tv_remote", {"keys": ["up"]}) == {"keys": ["up"]}
    assert canonical("tv_show", {"html": "x", "remote": ["keys"]}) == {"html": "x", "remote": ["keys"]}
    assert canonical("tv_show", None) is None
    # The tool description does not offer it.
    assert '"keys"' not in PublicTools.tv_show.__doc__ and '"keys"' not in PAGE_GUIDE


async def test_keys_alias_reaches_tv_show_through_mcp(remote, aiohttp_client):
    service, observer, control = remote
    shown = present_as(service, "mirror")
    client = await aiohttp_client(service.app())
    async with (
        httpx2.AsyncClient(headers={"Authorization": "Bearer " + service.state.token}) as http,
        streamable_http_client(str(client.make_url("/mcp")), http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        listed = {tool.name: tool for tool in (await session.list_tools()).tools}["tv_show"]
        assert listed.input_schema["properties"]["remote"]["enum"] == ["off", "focus", "tvos"]
        result = await session.call_tool("tv_show", {"html": "<h1>x</h1>", "remote": "keys"})
    assert not result.is_error and result.structured_content["data"]["remote"]["mode"] == "focus"
    assert shown == ["focus"]


# --- Dispatch --------------------------------------------------------------


def fake_frame(delivered=True, document="doc"):
    """A content frame whose press log counts each key the page got."""
    session = SimpleNamespace(send=AsyncMock())
    context = SimpleNamespace(new_cdp_session=AsyncMock(return_value=session))
    page = SimpleNamespace(evaluate=AsyncMock(), context=context)

    async def evaluate(script):
        if script == PRESS_LOG:
            return [frame.document, len(pressed(frame)) if delivered else 0, frame.helper]
        frame.helper = True

    frame = SimpleNamespace(page=page, session=session, evaluate=AsyncMock(side_effect=evaluate), document=document,
                            helper=False, url="http://127.0.0.1:8766/c/a/index.html")
    return frame


def pressed(frame):
    """The keys that went to a fake frame, from its key-down events."""
    return [call.args[1]["key"] for call in frame.session.send.await_args_list if call.args[1]["type"] != "keyUp"]


@pytest.mark.parametrize("mode,direct", [("keys", False), ("focus", False), ("keys", True), ("focus", True), ("tvos", False)])
async def test_key_goes_to_the_current_content_frame(remote, monkeypatch, mode, direct):
    service, observer, control = remote
    frame = fake_frame()
    control.interaction.binding = AsyncMock(return_value=(frame, frame.url, direct))
    control.mode = mode
    # Without the window for keys in a run, every key is on its own.
    monkeypatch.setattr(remote_input, "WAKE_FRESH_S", 0)
    for key, expected in remote_input.KEYS.items():
        assert await control.send(key) == 1
        assert pressed(frame)[-1] == expected
    assert set(remote_input.KEYS.values()) == {"ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Enter"}
    # Each press is a key-down and a key-up on one CDP session of the page.
    frame.page.context.new_cdp_session.assert_awaited_once_with(frame.page)
    sent = [call.args for call in frame.session.send.await_args_list]
    assert {method for method, _ in sent} == {"Input.dispatchKeyEvent"}
    assert sent[0][1] == {"type": "rawKeyDown", "key": "ArrowLeft", "code": "ArrowLeft", "windowsVirtualKeyCode": 37}
    assert sent[1][1] == {"type": "keyUp", "key": "ArrowLeft", "code": "ArrowLeft", "windowsVirtualKeyCode": 37}
    # Enter carries its text, which activates a button or a link.
    assert sent[8][1] == {"type": "keyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13,
                          "text": "\r", "unmodifiedText": "\r"}
    assert [event["type"] for _, event in sent] == ["rawKeyDown", "keyUp"] * 4 + ["keyDown", "keyUp"]
    # Keyboard focus is restored for every key: the stage iframe, then the helper.
    assert [c.args for c in frame.page.evaluate.await_args_list] == ([] if direct else [(FOCUS_STAGE,)] * 5)
    # A script runs in the content frame just before each key. Without it an idle frame drops the key.
    # The helper source goes out once, to a document that does not have it yet.
    helper = {"focus": [(FOCUS_HELPER,)], "tvos": [(TVOS_HELPER,)]}.get(mode, [])
    assert [c.args for c in frame.evaluate.await_args_list] == [(PRESS_LOG,), *helper, (PRESS_LOG,)] + [(PRESS_LOG,), (PRESS_LOG,)] * 4
    # A navigation gives a new document without the helper. It is sent again.
    frame.helper, frame.document = False, "next"
    frame.evaluate.reset_mock()
    await control.send("left")
    assert [c.args for c in frame.evaluate.await_args_list] == [(PRESS_LOG,), *helper, (PRESS_LOG,)]


@pytest.mark.parametrize("mode", ["keys", "tvos"])
async def test_keys_in_a_run_share_one_wake(remote, monkeypatch, mode):
    """Hardware finding: a key took 80 to 125 ms from the observer to the page, in five round trips."""
    service, observer, control = remote
    frame = fake_frame()
    control.interaction.binding = AsyncMock(return_value=(frame, frame.url, False))
    control.mode = mode
    helper = [(TVOS_HELPER,)] if mode == "tvos" else []

    def scripts():
        calls = [c.args for c in frame.evaluate.await_args_list], [c.args for c in frame.page.evaluate.await_args_list]
        frame.evaluate.reset_mock()
        frame.page.evaluate.reset_mock()
        return calls

    # The first key wakes the frame and gives the stage focus. The read after each key is the wake for the next.
    for key in ("right", "right", "down"):
        assert await control.send(key) == 1
    assert scripts() == ([(PRESS_LOG,), *helper, (PRESS_LOG,)] + [(PRESS_LOG,)] * 2, [(FOCUS_STAGE,)])
    # Keys that waited in the queue go out together: one read for all of them.
    assert await control.send("left", "left", "up") == 3
    assert scripts() == ([(PRESS_LOG,)], [])
    assert pressed(frame)[-3:] == ["ArrowLeft", "ArrowLeft", "ArrowUp"]
    assert [call.args[1]["type"] for call in frame.session.send.await_args_list[-6:]] == ["rawKeyDown", "keyUp"] * 3
    # Select can start a navigation. The key after it wakes the frame again.
    assert await control.send("select") == 1 and await control.send("left") == 1
    assert scripts() == ([(PRESS_LOG,)] * 3, [(FOCUS_STAGE,)])
    # So does a key after a new page was shown, even in a frame that stays the same object.
    await service.stage.show("a", "/c/a/index.html", remote=mode)
    assert await control.send("left") == 1
    assert scripts() == ([(PRESS_LOG,)] * 2, [(FOCUS_STAGE,)])
    # So does a key after one that the page did not get,
    frame.session.send.side_effect = lambda *_: frame.session.send.await_args_list.pop()
    assert await control.send("left") == 0
    frame.session.send.side_effect = None
    assert await control.send("left") == 1
    assert scripts() == ([(PRESS_LOG,)] * 3, [(FOCUS_STAGE,)])
    # and a key after the window.
    monkeypatch.setattr(remote_input, "WAKE_FRESH_S", 0.02)
    await asyncio.sleep(0.03)
    assert await control.send("left") == 1
    assert scripts() == ([(PRESS_LOG,)] * 2, [(FOCUS_STAGE,)])
    await control.send("left")
    assert scripts() == ([(PRESS_LOG,)], [])


async def test_a_key_the_page_did_not_get_is_reported(remote, caplog):
    service, observer, control = remote
    control.mode = "keys"
    lost = fake_frame(delivered=False)
    control.interaction.binding = AsyncMock(return_value=(lost, lost.url, False))
    assert not await control.send("left")
    # A key that starts a navigation gives a new document. That is not a lost key.
    lost.session.send.side_effect = lambda *_: setattr(lost, "document", "next")
    assert await control.send("select")
    lost.session.send.side_effect = None
    await service.stage.show("a", "/c/a/index.html", remote="keys")
    await until(lambda: control.state == "active")
    observer.emit({"event": "button", "key": "left"})
    await until(lambda: control.missed == 1)
    assert "did not get it" in caplog.text


async def test_forwarding_needs_no_tool_lock_and_survives_a_lost_key(remote):
    service, observer, control = remote
    frame = fake_frame()
    failure = Fault("stale_view", "The mirror no longer has the verified presentation.", "Show again.")
    control.interaction.binding = AsyncMock(side_effect=[failure, (frame, frame.url, False), (frame, frame.url, False)])
    await service.stage.show("a", "/c/a/index.html", remote="keys")
    await until(lambda: control.state == "active")
    # tv_remote holds the tool lock for seconds.
    async with service.tools.lock:
        for key in ("left", "right", "select"):
            observer.emit({"event": "button", "key": key, "received": time.time(), "device_time": time.time()})
            await asyncio.sleep(0.02)  # Each key is on its own. Keys that wait together go out together.
        await until(lambda: len(pressed(frame)) == 2)
    assert pressed(frame) == ["ArrowRight", "Enter"]
    assert control.presses == 3 and control.status()["last"]["key"] == "select"
    observer.emit({"event": "button", "key": "home"})
    assert control.presses == 3


EVENT_SCRIPT = """import json, sys, threading, time
def press():
    time.sleep(0.3)
    print(json.dumps({"event": "button", "key": "left"}), flush=True)
for line in sys.stdin:
    if json.loads(line)["method"] == "buttons_start":
        print(json.dumps({"event": "buttons", "state": "active"}), flush=True)
        threading.Thread(target=press, daemon=True).start()
    print(json.dumps({"ok": True}), flush=True)
"""


@pytest.mark.parametrize("wrapped", [False, True])
async def test_forwarding_does_not_wait_for_the_observer_request_lock(service, tmp_path, wrapped):
    """A screen read holds the observer lock for seconds. The event pipe must stay independent."""
    from atv_bridge_app.observer import ReceiverObserver

    client = ObserverClient("192.0.2.1", State(tmp_path), [sys.executable, "-c", EVENT_SCRIPT])
    observer = client
    if wrapped:
        # The production wrapper has its own lock on top of the client's.
        service.state.receiver = lambda kind: {"path": "x"}
        observer = ReceiverObserver(service.engine.adapter.resolver, service.state)
        observer.transport = client
    service.operations.observer = observer
    service.operations.presenter = SimpleNamespace(mode="mirror", started=True)
    control = service.tools.remote_input
    frame = fake_frame()
    control.interaction.binding = AsyncMock(return_value=(frame, frame.url, False))
    try:
        await service.stage.show("a", "/c/a/index.html", remote="keys")
        await until(lambda: control.state == "active")
        locks = [client.lock, service.tools.lock] + ([observer.lock] if wrapped else [])
        for lock in locks:
            await lock.acquire()
        try:
            await until(lambda: len(pressed(frame)) == 1)
            assert all(lock.locked() for lock in locks)
        finally:
            for lock in locks:
                lock.release()
    finally:
        await control.close()
        await client.close()


async def test_queued_keys_expire(remote, monkeypatch):
    service, observer, control = remote
    frame = fake_frame()
    release = asyncio.Event()

    async def binding():
        await release.wait()
        return frame, frame.url, False

    control.interaction.binding = binding
    monkeypatch.setattr(remote_input, "STALE_S", 0.05)
    await service.stage.show("a", "/c/a/index.html", remote="keys")
    await until(lambda: control.state == "active")
    for _ in range(remote_input.QUEUE_LIMIT + 4):
        observer.emit({"event": "button", "key": "down"})
    assert control.queue.qsize() <= remote_input.QUEUE_LIMIT
    await asyncio.sleep(0.15)
    release.set()
    observer.emit({"event": "button", "key": "up"})
    await until(lambda: len(pressed(frame)) == 1)
    await asyncio.sleep(0.02)
    assert pressed(frame) == ["ArrowUp"]


# --- Real browser ----------------------------------------------------------

CONTENT = """<style>button,a,div[tabindex]{position:absolute;width:200px;height:80px}</style>
<button id=a style='left:0;top:0' onclick='hit(this)'>A</button>
<button id=b style='left:400px;top:0' onclick='hit(this)'>B</button>
<a id=c href='/next' style='left:0;top:300px' onclick='hit(this); return false'>C</a>
<div id=d tabindex=0 style='left:400px;top:300px' onclick='hit(this)'>D</div>
<div id=own tabindex=0 style='left:800px;top:0'>Own</div>
<button id=wide style='left:0;top:600px;width:1000px' onclick='hit(this)'>Wide</button>
<script>
window.clicks = {}; window.keys = [];
function hit(el) { clicks[el.id] = (clicks[el.id] || 0) + 1; }
addEventListener('keydown', e => keys.push([e.key, e.isTrusted]), true);
// This widget handles the arrows itself.
own.addEventListener('keydown', e => { if (e.key.startsWith('Arrow')) e.preventDefault(); });
</script>"""


# A page laid out for the tvos mode: a grid, a row with an offset neighbor, two marked rows,
# a wide banner and a strip that scrolls sideways.
TVOS_CONTENT = """<style>
body{margin:0;background:#111} .c,button,a{position:absolute;width:200px;height:100px;border:0;background:#333}
#strip{position:absolute;left:40px;top:860px;width:1100px;height:140px;overflow-x:auto;white-space:nowrap}
#strip button{position:static;display:inline-block;width:300px;margin-right:20px}
</style>
<button id=g00 style='left:40px;top:20px'></button><button id=g01 style='left:300px;top:20px'></button><button id=g02 style='left:560px;top:20px'></button>
<button id=g10 style='left:40px;top:150px'></button><button id=g11 style='left:300px;top:150px'></button><button id=g12 style='left:560px;top:150px'></button>
<button id=r0 style='left:40px;top:290px'></button><button id=r1 style='left:300px;top:290px'></button>
<button id=off style='left:560px;top:330px'></button><button id=diag style='left:430px;top:405px'></button>
<div data-tv-group><button id=a0 style='left:40px;top:540px'></button><button id=a1 style='left:300px;top:540px'></button><button id=a2 style='left:560px;top:540px'></button></div>
<div data-tv-group><button id=b0 style='left:40px;top:660px'></button><button id=b1 style='left:300px;top:660px'></button><button id=b2 style='left:560px;top:660px'></button></div>
<button id=wide style='left:40px;top:780px;width:800px;height:60px' onclick='clicks.wide=(clicks.wide||0)+1'></button>
<div id=own tabindex=0 class=c style='left:900px;top:20px'></div>
<div id=strip>""" + "".join(f"<button id=s{n}></button>" for n in range(12)) + """</div>
<script>
window.clicks = {}; window.scrollEnds = 0;
strip.addEventListener('scrollend', () => scrollEnds++);
own.addEventListener('keydown', e => { if (e.key.startsWith('Arrow')) e.preventDefault(); });
</script>"""


def rows_page(*tiles, style=""):
    """Rows with the given numbers of tiles, as on the page of the hardware test. Each row scrolls sideways on its own.

    One column is 320 px: a 300 px tile and a 20 px gap. The viewport shows four columns.
    """
    return """<style>
body{margin:0;background:#111} .row{display:flex;gap:20px;overflow-x:auto;padding:30px 0;scroll-behavior:smooth}
.row button{flex:none;width:300px;height:160px;border:0;background:#333}""" + style + "</style>" + "".join(
        f"<div class=row id=row{row} data-tv-group>" + "".join(f"<button id=t{row}{column}></button>" for column in range(count)) + "</div>"
        for row, count in enumerate(tiles))


# Three rows of eight tiles.
ROWS_CONTENT = rows_page(8, 8, 8)


@pytest.fixture
async def browser_frame(request):
    from playwright.async_api import async_playwright

    content = getattr(request, "param", CONTENT)

    async with async_playwright() as driver:
        browser = None
        for options in ({}, {"channel": "chrome"}):  # Bundled Chromium, then an installed Chrome.
            try:
                browser = await driver.chromium.launch(**options)
                break
            except Exception:  # noqa: BLE001 - The browser is optional on a development host.
                continue
        if browser is None:
            pytest.skip("Chromium is unavailable")
        page = await browser.new_page(viewport={"width": 1280, "height": 720 if content is CONTENT else 1000})
        # The stage shows bundles in a sandboxed iframe without allow-same-origin.
        await page.set_content(
            "<style>iframe{position:absolute;inset:0;width:100%;height:100%;border:0}</style>"
            "<iframe class=visible sandbox='allow-scripts allow-forms' srcdoc=\"" + content.replace('"', "&quot;") + '"></iframe>'
        )
        frame = page.frames[1]
        await frame.wait_for_selector("button")
        yield frame
        await browser.close()


async def test_keys_reach_the_sandboxed_frame_as_trusted_events(remote, browser_frame):
    service, observer, control = remote
    frame = browser_frame
    control.interaction.binding = AsyncMock(return_value=(frame, frame.url, False))
    control.mode = "keys"
    for key in ("left", "right", "up", "down", "select"):
        await control.send(key)
    assert await frame.evaluate("keys") == [[k, True] for k in ("ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Enter")]
    assert await frame.evaluate("document.hasFocus()") and await frame.evaluate("clicks") == {}
    # The page keeps a small press log that tv_page_run can read.
    log = await frame.evaluate("window.__tvRemote")
    assert (log["count"], log["last"]) == (5, "Enter")


async def test_keys_sent_together_arrive_in_order(remote, browser_frame):
    """Key-down and key-up events go out without a wait between them. The page must still get them in order."""
    service, observer, control = remote
    frame = browser_frame
    control.interaction.binding = AsyncMock(return_value=(frame, frame.url, False))
    control.mode = "keys"
    await frame.evaluate("""() => { window.order = [];
        for (const type of ['keydown', 'keyup']) addEventListener(type, e => order.push(type + ' ' + e.key + ' ' + e.isTrusted), true); }""")
    keys = ["left", "right", "up", "down", "right", "left", "down", "up"] * 6 + ["select"]
    assert await control.send(*keys) == len(keys)
    assert await frame.evaluate("order") == [f"{kind} {remote_input.KEYS[key]} true" for key in keys for kind in ("keydown", "keyup")]
    # One at a time, as most keys arrive.
    await frame.evaluate("order.length = 0")
    for key in keys[:8]:
        assert await control.send(key) == 1
    assert await frame.evaluate("order") == [f"{kind} {remote_input.KEYS[key]} true" for key in keys[:8] for kind in ("keydown", "keyup")]
    assert await frame.evaluate("clicks") == {}


async def test_the_debug_log_shows_where_the_time_of_a_key_goes(remote, browser_frame, caplog):
    """Hardware finding: the app took 79 to 260 ms from the observer's read to the end of its send.

    That figure covered five CDP round trips, one after the other: stage focus, wake script, key-down,
    key-up and the press log read. The key was in the page after the third. Now the stage focus runs
    beside the wake script, key-up follows key-down without a wait, and a key in a run needs no wake.
    """
    service, observer, control = remote
    frame = browser_frame
    control.interaction.binding = AsyncMock(return_value=(frame, frame.url, False))
    caplog.set_level("DEBUG", logger="atv_bridge_app.present.remote_input")
    await service.stage.show("a", "/c/a/index.html", remote="focus")
    await until(lambda: control.state == "active")

    def lines():
        return [record.getMessage() for record in caplog.records if "reached the page" in record.getMessage()]

    def press(*keys, gesture=None):
        for key in keys:
            event = {"event": "button", "key": key, "received": time.time(), "device_time": time.time() - 0.04}
            observer.emit(event | ({"gesture": gesture} if gesture else {}))

    pattern = (r"Remote key (.+) reached the page (\d+) ms after the observer read it "
               r"\(queue (\d+), bind (\d+), wake (\d+), keys (\d+) ms; press log read (\d+) ms more; TV log to observer (\d+) ms\)")

    def parsed(line):
        names, *numbers = re.fullmatch(pattern, line).groups()
        return names, dict(zip(("total", "queue", "bind", "wake", "keys", "log", "tv"), map(int, numbers)))

    # A key on its own: the wake script runs first.
    press("right")
    await until(lambda: len(lines()) == 1)
    names, first = parsed(lines()[0])
    assert names == "right" and first["tv"] in (39, 40, 41)
    assert first["total"] <= first["queue"] + first["bind"] + first["wake"] + first["keys"] + 4  # Whole milliseconds.
    # The arrows of a flick arrive while the first one goes out. They leave together and need no wake.
    press("right", gesture="swipe")
    press("right", "right", gesture="momentum")
    await until(lambda: len(lines()) >= 2 and "momentum" in lines()[-1])
    names, last = parsed(lines()[-1])
    assert names.endswith("right (momentum) + right (momentum)") and last["wake"] == 0
    assert await frame.evaluate("document.activeElement.id") == "own"
    assert await frame.evaluate("window.__tvRemote.count") == 4 and control.missed == 0


async def test_focus_helper_moves_by_geometry_and_enter_clicks_once(remote, browser_frame):
    service, observer, control = remote
    frame = browser_frame
    control.interaction.binding = AsyncMock(return_value=(frame, frame.url, False))
    control.mode = "focus"

    async def press(key):
        await control.send(key)
        return await frame.evaluate("document.activeElement.id")

    assert await press("right") == "a"  # The first arrow picks the first item on screen.
    assert await press("right") == "b"
    assert await press("down") == "d"
    assert await press("left") == "c"
    assert await press("up") == "a"
    assert await press("left") == "a"  # Nothing that way.
    assert await press("select") == "a"
    assert await press("down") == "c"
    await press("select")
    await press("right")
    await press("select")
    # One click per Select: native for the button and link, from the helper for the div.
    assert await frame.evaluate("clicks") == {"a": 1, "c": 1, "d": 1}
    assert await frame.evaluate("getComputedStyle(document.activeElement).outlineStyle") == "solid"
    # A wider item in the next row is not "right", but it is "down" from either column.
    assert await press("down") == "wide"
    assert await press("right") == "wide"
    assert await press("up") == "c"
    assert await press("right") == "d"
    assert await press("down") == "wide"
    assert await press("up") == "c"
    assert await press("up") == "a"
    assert await press("right") == "b"
    # The helper leaves a page that handles arrows alone.
    assert await press("right") == "own"
    assert await press("left") == "own"
    assert await frame.evaluate("window.__tvRemoteFocus") is True


@pytest.fixture
async def tvos(remote, browser_frame):
    """Send keys through RemoteInput in tvos mode. Count how often the helper source is sent."""
    service, observer, control = remote
    frame = browser_frame
    sent = []

    async def evaluate(script, *args):
        sent.append(script)
        return await frame.evaluate(script, *args)

    proxy = SimpleNamespace(page=frame.page, evaluate=evaluate, url=frame.url)
    control.interaction.binding = AsyncMock(return_value=(proxy, frame.url, False))
    control.mode = "tvos"

    async def press(*keys, start=None):
        if start:
            await frame.evaluate(f"document.getElementById('{start}').focus({{preventScroll: true}})")
        for key in keys:
            assert await control.send(key)
        return await frame.evaluate("document.activeElement.id")

    async def scroll(**rows):
        """Put each named row at a scroll offset, at once."""
        await frame.evaluate("rows => Object.entries(rows).forEach(([id, left]) => "
                             "document.getElementById(id).scrollTo({left, behavior: 'instant'}))", rows)

    return SimpleNamespace(frame=frame, press=press, sent=sent, scroll=scroll, control=control)


TVOS_PAGE = pytest.mark.parametrize("browser_frame", [TVOS_CONTENT], indirect=True)
ROWS_PAGE = pytest.mark.parametrize("browser_frame", [ROWS_CONTENT], indirect=True)


@TVOS_PAGE
async def test_tvos_beam_search_in_a_grid_and_past_an_offset_neighbor(tvos):
    assert await tvos.press("right") == "g00"  # The first arrow picks the first item on screen.
    assert await tvos.press("right") == "g01"
    assert await tvos.press("down") == "g11"
    assert await tvos.press("right") == "g12"
    assert await tvos.press("up") == "g02"
    assert await tvos.press("left", "left") == "g00"
    assert await tvos.press("left") == "g00"  # Nothing that way.
    # Right of r1: "off" overlaps the beam. "diag" is closer by center but outside the beam.
    assert await tvos.press("right", start="r1") == "off"
    # The focus helper's scoring would pick the other one. It stays as the fallback when the beam is empty.
    assert await tvos.press("down", start="r1") == "diag"
    assert await tvos.press("left", start="diag") == "r1"
    # The helper source crossed CDP once for all of these 11 keys. Each key has one press log read
    # after it. Only a key that follows a pause has one before it too.
    assert tvos.sent.count(TVOS_HELPER) == 1 and 12 <= tvos.sent.count(PRESS_LOG) < 22
    assert await tvos.frame.evaluate("window.__tvRemoteFocus") == "tvos"


@TVOS_PAGE
async def test_tvos_rows_do_not_remember_their_last_item(tvos):
    assert await tvos.press("right", "right", start="a0") == "a2"
    assert await tvos.press("down") == "b2"
    assert await tvos.press("left", "left") == "b0"
    # Back into the first row: the item straight above, not the one that had focus there last.
    assert await tvos.press("up") == "a0"
    assert await tvos.press("down") == "b0"
    # The same for a scroll container.
    assert await tvos.press("right", "right", start="s0") == "s2"
    assert await tvos.press("up") == "wide"
    # Two tiles lie fully below the wide banner. The one nearer to its center wins.
    assert await tvos.press("down") == "s1"
    # s1 and s2 lie below the banner and are not "right" of it. s3 starts past its right end.
    assert await tvos.press("right", start="wide") == "s3"


@TVOS_PAGE
async def test_tvos_look_lifts_the_focused_item(tvos):
    frame = tvos.frame
    look = "(() => { const s = getComputedStyle(document.activeElement); return [s.scale, s.outlineStyle, s.outlineWidth, s.outlineColor, s.boxShadow !== 'none']; })()"
    assert await tvos.press("right", start="g00") == "g01"
    await asyncio.sleep(0.45)
    assert await frame.evaluate(look) == ["1.06", "solid", "3px", "rgb(255, 255, 255)", True]
    assert await frame.evaluate("getComputedStyle(g00).scale") == "none"
    # The lift does not widen the beam: the box is measured without its scale.
    assert await tvos.press("down") == "g11"
    # A wide item lifts less.
    await asyncio.sleep(0.3)
    assert await tvos.press("down", start="b0") == "wide"
    await asyncio.sleep(0.45)
    assert (await frame.evaluate(look))[0] == "1.03"
    # Select presses the item: scale 1 for a moment, one click.
    assert await tvos.press("select") == "wide"
    assert await frame.evaluate("document.documentElement.classList.contains('tv-remote-pressed')")
    await asyncio.sleep(0.1)
    assert float((await frame.evaluate(look))[0]) < 1.01
    await asyncio.sleep(0.5)
    assert not await frame.evaluate("document.documentElement.classList.contains('tv-remote-pressed')")
    assert (await frame.evaluate(look))[0] == "1.03" and await frame.evaluate("clicks") == {"wide": 1}
    # Every duration and scale is in one config object. The values suit about 10 mirrored frames per second.
    config = await frame.evaluate("window.__tvRemoteConfig")
    assert (config["scale"], config["wideScale"], config["scaleInMs"], config["scaleOutMs"], config["pressedMs"], config["fastRunMs"]) == (1.06, 1.03, 180, 260, 300, 250)
    assert (config["scrollMarginY"], config["scrollMarginX"], config["scrollBehavior"]) == (60, 80, "instant")
    # Only the scale animates. A shadow or outline transition would repaint on every frame.
    assert await frame.evaluate("getComputedStyle(document.activeElement).transitionProperty") == "scale"



@TVOS_PAGE
async def test_tvos_keeps_the_config_of_the_page_and_applies_later_changes(tvos):
    """Review finding: the helper replaced a config that the page had set before it started."""
    frame = tvos.frame
    color = "getComputedStyle(document.activeElement).outlineColor"
    await frame.evaluate("window.__tvRemoteConfig = {ringColor: 'rgb(255, 0, 0)', pressedMs: 50}")
    assert await tvos.press("right", start="g00") == "g01"
    assert await frame.evaluate(color) == "rgb(255, 0, 0)"
    assert await frame.evaluate("[__tvRemoteConfig.pressedMs, __tvRemoteConfig.fastRunMs]") == [50, 250]
    # A later change applies at once, also to the look.
    await frame.evaluate("__tvRemoteConfig.ringColor = 'rgb(0, 255, 0)'; __tvRemoteConfig.scale = 1.2")
    assert await frame.evaluate(color) == "rgb(0, 255, 0)"
    await asyncio.sleep(0.45)
    assert await frame.evaluate("getComputedStyle(document.activeElement).scale") == "1.2"

@TVOS_PAGE
async def test_tvos_flick_lands_on_the_right_item_and_scrolls_at_once(tvos):
    frame = tvos.frame
    inside = """(() => { const r = document.activeElement.getBoundingClientRect(), box = strip.getBoundingClientRect();
        return [r.left >= box.left && r.right <= box.right + 1, strip.scrollLeft]; })()"""
    await tvos.press(start="s0")
    # Four quick arrows, as after a flick.
    assert await tvos.press("right", "right", "right", "right") == "s4"
    # The strip is already where it must be. No scroll is still running when the next key arrives.
    visible, left = await frame.evaluate(inside)
    assert visible and left > 0
    # During the run the items show only the ring.
    assert await frame.evaluate("document.documentElement.classList.contains('tv-remote-fast')")
    assert await frame.evaluate("getComputedStyle(document.activeElement).scale") == "none"
    await asyncio.sleep(1.0)
    # The last item lifts, and the strip did not move again.
    assert not await frame.evaluate("document.documentElement.classList.contains('tv-remote-fast')")
    assert await frame.evaluate("getComputedStyle(s4).scale") == "1.06"
    assert await frame.evaluate(inside) == [True, left]


@pytest.mark.parametrize("browser_frame,mode,expected", [(TVOS_CONTENT, "tvos", "none"), (TVOS_CONTENT, "focus", "auto")],
                         indirect=["browser_frame"])
async def test_tvos_hides_scrollbars_and_focus_mode_keeps_them(tvos, mode, expected):
    tvos.control.mode = mode
    assert await tvos.press("right", start="s0") == "s1"
    # The strip scrolls sideways. The document itself can scroll too.
    assert await tvos.frame.evaluate(
        "[strip, document.documentElement].map(el => getComputedStyle(el).scrollbarWidth)") == [expected] * 2


@TVOS_PAGE
async def test_tvos_leaves_a_page_that_handles_arrows_alone(tvos):
    assert await tvos.press("right", start="g02") == "own"
    assert await tvos.press("left", "down") == "own"


@ROWS_PAGE
async def test_tvos_up_and_down_go_straight_when_rows_are_scrolled_by_whole_columns(tvos):
    """Hardware finding: up and down went diagonally, to the item that had focus in that row before."""
    await tvos.scroll(row1=320, row2=640)
    for column in (1, 2):
        # The tile straight below has another index in each row.
        assert await tvos.press("down", start=f"t0{column}") == f"t1{column + 1}"
        assert await tvos.press("down") == f"t2{column + 2}"
        assert await tvos.press("up") == f"t1{column + 1}"
        assert await tvos.press("up") == f"t0{column}"
    # A walk along one row does not change where up and down go from another row.
    assert await tvos.press("right", "down", start="t11") == "t23"
    assert await tvos.press("up", "up") == "t01"
    assert await tvos.frame.evaluate("[row0, row1, row2].map(row => row.scrollLeft)") == [0, 320, 640]
    # A tile at the edge scrolls into view with its margin at once, although the page asks for
    # smooth scrolling. The next key then reads boxes that no longer move.
    assert await tvos.press("right", "right", start="t01") == "t03"
    assert await tvos.frame.evaluate("[row0.scrollLeft, t03.getBoundingClientRect().right]") == [60, 1200]


# The tile of the row above (-1) or below (1) that covers most of the focused tile, without its lift.
STRAIGHT = """step => {
  const active = document.activeElement, box = active.getBoundingClientRect();
  const center = (box.left + box.right) / 2, half = active.offsetWidth / 2;
  const rows = [row0, row1, row2];
  let best = null, most = 0;
  for (const tile of rows[rows.indexOf(active.parentElement) + step].children) {
    const r = tile.getBoundingClientRect();
    const covered = Math.min(r.right, center + half) - Math.max(r.left, center - half);
    if (covered > most) [best, most] = [tile.id, covered];
  }
  return best;
}"""


@ROWS_PAGE
@pytest.mark.parametrize("offsets", [{"row1": 320, "row2": 640}, {"row1": 570, "row2": 130}, {"row1": 1260, "row2": 0}])
async def test_tvos_up_and_down_go_straight_from_every_column(tvos, offsets):
    """Also from the edge columns, where the scroll margin moves a row when its tile gets focus."""
    for column in range(4):
        await tvos.scroll(row0=0, **offsets)
        await tvos.press(start=f"t0{column}")
        for key, step in (("down", 1), ("down", 1), ("up", -1), ("up", -1)):
            straight = await tvos.frame.evaluate(STRAIGHT, step)
            assert straight and await tvos.press(key) == straight, (column, key)


@ROWS_PAGE
@pytest.mark.parametrize("mode", ["tvos", "focus"])
async def test_up_and_down_take_the_item_that_covers_most_when_rows_are_scrolled_apart(tvos, mode):
    """Two tiles of the next row lie partly below the focused one. The one that covers more of it wins."""
    tvos.control.mode = mode
    # Row 1 is 250 px further along: t12 is at 70-370, t13 at 390-690 and t14 at 710-1010.
    await tvos.scroll(row1=570)
    assert await tvos.press("down", start="t01") == "t13"  # t01 is at 320-620.
    assert await tvos.press("up") == "t01"
    assert await tvos.press("down", start="t02") == "t14"  # t02 is at 640-940.
    assert await tvos.press("up") == "t02"
    # From the other row: t13 at 390-690 lies below t01 (320-620), not below t02.
    assert await tvos.press("up", start="t13") == "t01"
    assert await tvos.frame.evaluate("window.__tvRemoteFocus") == (True if mode == "focus" else "tvos")


@ROWS_PAGE
async def test_tvos_down_right_after_a_flick_reads_the_row_where_it_ends(tvos):
    """With a smooth scroll, Down read the boxes before the row had moved.

    Measured in Chromium with scrollBehavior 'smooth': focus went to t16, below the place where the
    flicked tile had been, and row 1 then scrolled 570 px to show it. The tile straight below was on screen.
    """
    await tvos.scroll(row1=450)
    assert await tvos.press("right", "right", "right", "right", "down", start="t01") == "t14"
    assert await tvos.frame.evaluate("[row0.scrollLeft, row1.scrollLeft]") == [700, 450]


@ROWS_PAGE
async def test_tvos_left_and_right_stay_in_the_row_at_its_end(tvos):
    """After a flick, momentum arrows can arrive at the end of a row. They must not jump to another row."""
    await tvos.scroll(row0=9999)
    # Row 1 is not scrolled, so its tiles t14 to t17 lie further right than the last tile of row 0.
    assert await tvos.press("right", start="t07") == "t07"
    assert await tvos.press("right", "right") == "t07"
    assert await tvos.press("left", start="t10") == "t10"
    # Up and down still leave the row, and the row below scrolls to show the tile.
    assert await tvos.press("down", start="t07") == "t13"



# A focusable card with a button in its lower right corner.
CARD_CONTENT = """<style>body{margin:0;background:#111} div,button{position:absolute;border:0;background:#333}</style>
<div id=card tabindex=0 style='left:40px;top:20px;width:600px;height:300px'><button id=play style='right:20px;bottom:20px;width:80px;height:60px'></button></div>"""


@pytest.mark.parametrize("browser_frame", [CARD_CONTENT], indirect=True)
async def test_tvos_moves_from_a_card_to_the_button_inside_it(tvos):
    """Review finding: an item that does not reach past the focused box left the next row empty, and the search threw."""
    assert await tvos.press("right", start="card") == "play"
    assert await tvos.press("down", start="card") == "play"

# A menu at the left, outside every row, and two rows of tiles that scroll sideways. The rows lie lower than the menu.
MENU_CONTENT = """<style>body{margin:0;background:#111} button{border:0;background:#333}
#menu{position:absolute;left:20px;top:20px;width:200px} #menu button{display:block;width:200px;height:60px;margin-bottom:20px}
#content{position:absolute;left:300px;top:0;width:900px} .row{display:flex;gap:20px;overflow-x:auto;padding:30px 0}
.row button{flex:none;width:300px;height:160px}</style>
<div id=menu><button id=m0></button><button id=m1></button><button id=m2></button></div>
<div id=content><div style='height:330px'></div>""" + "".join(
    f"<div class=row id=row{row}>" + "".join(f"<button id=t{row}{column}></button>" for column in range(6)) + "</div>"
    for row in range(2)) + "</div>"


@pytest.mark.parametrize("browser_frame", [MENU_CONTENT], indirect=True)
async def test_tvos_left_and_right_go_between_a_menu_and_the_rows(tvos):
    """Review finding: Left from the first tile and Right from the menu did nothing, because only one of them is in a row.

    On a TV, Left at the start of a row goes to the side menu, however far up or down its items are.
    The menu item nearest in height wins.
    """
    assert await tvos.press("left", start="t00") == "m2"
    assert await tvos.press("right") == "t00"
    assert await tvos.press("left", start="t10") == "m2"  # t10 is at 580-740, m2 at 180-240.
    assert await tvos.press("right", start="m0") == "t00"
    assert await tvos.press("right", start="m1") == "t00"
    # In a row with more tiles that way, Left takes the next tile, although it is scrolled out of view.
    await tvos.scroll(row1=320)
    assert await tvos.press("left", start="t11") == "t10"
    # At the end of a row, Right still does not go to the other row.
    await tvos.scroll(row0=9999)
    assert await tvos.press("right", start="t05") == "t05"


# A page area that scrolls up and down, with one item outside it.
SCROLLER_CONTENT = """<style>body{margin:0;background:#111} button{position:absolute;width:200px;height:100px}
#main{position:relative;height:600px;overflow-y:auto}</style>
<div id=main><button id=inside style='left:400px;top:200px'></button><div style='height:2000px'></div></div>
<button id=outside style='left:900px;top:700px'></button>"""


@pytest.mark.parametrize("browser_frame", [SCROLLER_CONTENT], indirect=True)
async def test_tvos_only_a_sideways_scroller_keeps_left_and_right(tvos):
    # Nothing lies straight right of the item. The nearest item that way is outside the scrolling area.
    # A container that scrolls up and down is no row, so focus may leave it.
    assert await tvos.frame.evaluate("getComputedStyle(main).overflowX") == "auto"
    assert await tvos.press("right", start="inside") == "outside"
    assert await tvos.press("left") == "inside"


@pytest.mark.parametrize("browser_frame", [rows_page(8, 2, 8), rows_page(8, 8, 8, style="#row1{width:620px}")],
                         indirect=True, ids=["short", "narrow"])
async def test_tvos_up_and_down_take_the_next_row_when_nothing_of_it_lies_straight_ahead(tvos):
    """Up from column 6 went two rows up, to the tile straight above it there. tvOS takes the nearest tile of the next row.

    The middle row has two tiles, or eight in a scroller that shows two. Its tiles past that edge are
    hidden, so they do not count, although one of them lies straight ahead.
    """
    await tvos.scroll(row2=1260)  # t26 is at 660-960 and t27 at 980-1280. t11 ends at 620.
    assert await tvos.press("up", start="t26") == "t11"  # Not t02 at 640-940 in the first row.
    await tvos.scroll(row1=0, row2=1260)
    assert await tvos.press("down", start="t03") == "t11"  # Not t27 in the last row.
    assert await tvos.press("down") == "t25"
    assert await tvos.press("up") == "t11"
    assert await tvos.press("up") == "t01"


# Rows of narrow tiles. Every second row is half a column further right, so no tile lies straight
# above or below another one. The rows two apart line up.
STAGGERED_ROWS_CONTENT = """<style>
body{margin:0;background:#111} .row{display:flex;gap:180px;padding:30px 0}
.row button{flex:none;width:120px;height:100px;border:0;background:#333}
</style>""" + "".join(
    f"<div class=row data-tv-group style='padding-left:{210 if row % 2 else 40}px'>"
    + "".join(f"<button id=t{row}{column}></button>" for column in range(4)) + "</div>" for row in range(4))


@pytest.mark.parametrize("browser_frame", [STAGGERED_ROWS_CONTENT], indirect=True)
async def test_tvos_up_and_down_zigzag_through_a_staggered_grid(tvos):
    # t01 is at 340-460. Below it, t10 ends at 330 and t11 starts at 510. t21 lies straight below, two rows down.
    assert await tvos.press("down", start="t01") == "t10"
    assert await tvos.press("down") == "t21"
    assert await tvos.press("down") == "t30"
    assert await tvos.press("up") == "t21"
    assert await tvos.press("up") == "t10"
    assert await tvos.press("up") == "t01"
    assert await tvos.press("down", start="t03") == "t12"


# Three columns of tiles. The middle column is 60 px lower, so its tiles lie partly beside those of
# the other columns. The right column ends after one tile.
STAGGERED_COLUMNS_CONTENT = """<style>
body{margin:0;background:#111} button{position:absolute;width:200px;height:100px;border:0;background:#333}
</style>""" + "".join(
    f"<button id=c{column}{row} style='left:{40 + 240 * column}px;top:{20 + 60 * (column == 1) + 140 * row}px'></button>"
    for column, count in enumerate((3, 3, 1)) for row in range(count))


@pytest.mark.parametrize("browser_frame", [STAGGERED_COLUMNS_CONTENT], indirect=True)
async def test_tvos_up_and_down_stay_in_a_staggered_column(tvos):
    # c10 at 80-180 starts nearer below c00 at 20-120, but c01 at 160-260 lies straight below.
    assert await tvos.press("down", start="c00") == "c01"
    assert await tvos.press("down") == "c02"
    assert await tvos.press("up", "up") == "c00"
    assert await tvos.press("down", start="c10") == "c11"
    assert await tvos.press("down") == "c12"
    assert await tvos.press("up", "up") == "c10"
    # At the end of a column, the nearest tile below wins, although it lies partly beside.
    assert await tvos.press("down", start="c20") == "c10"


# Two rows of three tiles. The page prefers the middle tile of the second row.
AUTOFOCUS_CONTENT = """<style>body{margin:0;background:#111} button{position:absolute;width:200px;height:100px;border:0;background:#333}</style>
<button id=a0 style='left:40px;top:40px'></button><button id=a1 style='left:300px;top:40px'></button><button id=a2 style='left:560px;top:40px'></button>
<button id=b0 style='left:40px;top:200px'></button><button id=b1 autofocus style='left:300px;top:200px'></button><button id=b2 style='left:560px;top:200px'></button>"""
AUTOFOCUS_PAGE = pytest.mark.parametrize("browser_frame,mode", [(AUTOFOCUS_CONTENT, "tvos"), (AUTOFOCUS_CONTENT, "focus")],
                                         indirect=["browser_frame"])


@AUTOFOCUS_PAGE
async def test_the_autofocus_item_shows_focus_when_the_page_shows_and_the_first_arrow_moves_from_it(tvos, mode):
    """Review finding: the first arrow went to the first item on screen, past the page's autofocus item.

    Chromium blocks autofocus in the stage's cross-origin frame. tvOS shows its preferred item focused
    when a view appears, and the first press moves from it.
    """
    frame, control = tvos.frame, tvos.control
    control.mode = mode
    assert await frame.evaluate("document.activeElement === document.body")
    await control.prime()
    # The item has focus, and the frame has system focus, before any press. Without it, :focus did not match.
    assert await frame.evaluate("[document.activeElement.id, document.hasFocus(), window.__tvRemote.count]") == ["b1", True, 0]
    await asyncio.sleep(0.45)
    look = "(() => { const s = getComputedStyle(b1); return [s.scale, s.outlineStyle]; })()"
    assert await frame.evaluate(look) == [{"tvos": "1.06", "focus": "none"}[mode], "solid"]
    assert await tvos.press("right") == "b2"


@AUTOFOCUS_PAGE
async def test_with_nothing_focused_the_first_arrow_lands_on_the_autofocus_item(tvos, mode):
    """A document that shows no focus yet, such as one after a navigation, shows it at the first arrow."""
    tvos.control.mode = mode
    assert await tvos.press("right") == "b1"  # Not a0, the first item on screen.
    assert await tvos.press("up") == "a1"


# --- Show latency ----------------------------------------------------------

CDP_DELAY_S = 0.5  # One CDP round trip on a busy host.
WATCH_DELAY_S = 3  # How long a slow TV takes to accept the log stream.


def slow_frame(frame, delay=CDP_DELAY_S):
    """A real frame on which every CDP round trip takes `delay` longer."""

    async def later(call, *args):
        await asyncio.sleep(delay)
        return await call(*args)

    async def new_cdp_session(_page):
        session = await later(frame.page.context.new_cdp_session, frame.page)
        return SimpleNamespace(send=lambda *args: later(session.send, *args))

    page = SimpleNamespace(evaluate=lambda *args: later(frame.page.evaluate, *args),
                           context=SimpleNamespace(new_cdp_session=new_cdp_session))
    return SimpleNamespace(page=page, url=frame.url, evaluate=lambda *args: later(frame.evaluate, *args))


@pytest.mark.parametrize("browser_frame,path", [(AUTOFOCUS_CONTENT, "stage"), (AUTOFOCUS_CONTENT, "top_level")],
                         indirect=["browser_frame"])
async def test_show_does_not_wait_for_remote_input(remote, browser_frame, aiohttp_server, path):
    """Review finding: tv_show waited up to 3 s for the first key setup and up to 3 s for the watch to start.

    A warm page change on a Home Assistant Green took 2.6 to 2.8 s. Both now run after the show returns.
    Here the TV takes 3 s to accept the log stream, and each CDP round trip takes 500 ms.
    """
    service, observer, control = remote
    # The real observer answers buttons_start at once. It reports the watch when the TV accepts the stream.
    observer.reply = None
    plain = observer.call

    async def call(method, **params):
        if method == "buttons_start":
            asyncio.get_running_loop().call_later(WATCH_DELAY_S, observer.emit, {"event": "buttons", "state": "active"})
        return await plain(method, **params)

    observer.call = call
    # The stage shows a page in its frame. A page that refuses a frame shows at the top level.
    direct = path == "top_level"
    content = browser_frame.page.main_frame if direct else browser_frame
    frame = slow_frame(content)

    async def binding():
        await asyncio.sleep(CDP_DELAY_S)  # The bridge finds the frame over CDP.
        return frame, frame.url, direct

    control.interaction.binding = binding

    async def site(request):
        return web.Response(text="<h1>Live</h1>", headers={"X-Frame-Options": "DENY"}, content_type="text/html")

    app = web.Application()
    app.router.add_get("/", site)
    server = await aiohttp_server(app)
    service.stage.stage_ports.add(server.port)
    mirror = service.operations.live_mirror
    mirror.start, mirror.verify_document = AsyncMock(), AsyncMock()
    mirror.confirm_frame = AsyncMock(return_value=True)
    source, mode = ({"url": str(server.make_url("/"))}, "focus") if direct else ({"html": "<h1>x</h1>"}, "tvos")

    async def show(**args):
        started = time.monotonic()
        result = await service.tools.call("tv_show", source | args)
        assert result["ok"] and service.stage.direct_browser is (mirror if direct else None)
        return result, time.monotonic() - started

    async def look():
        return (await service.tools.call("tv_look", {}))["screen"]["presenting"]["remote"]

    await show(remote="off")  # The first show also stores the page.
    _, off = await show(remote="off")
    first, on = await show()
    early = await look()
    # The key setup still runs, after the show. Without a press, the page's autofocus item shows focus.
    done = "window.__tvRemoteFocus" + ("" if direct else " && document.activeElement.id === 'b1'")
    async with asyncio.timeout(5):
        while not await content.evaluate(f"Boolean({done})"):
            await asyncio.sleep(0.05)
    await until(lambda: control.state == "active", timeout=WATCH_DELAY_S + 1)
    later = await look()
    # The next page finds the watch running.
    change, changed = await show()
    timings = f"{path}: off {off * 1000:.0f} ms, first show {on * 1000:.0f} ms, page change {changed * 1000:.0f} ms"
    print(timings)
    assert first["data"]["remote"] == {"mode": mode, "state": "starting"} and early["state"] == "starting"
    assert later["state"] == "active" and change["data"]["remote"] == {"mode": mode, "state": "active"}
    assert on - off < 0.1 and changed - off < 0.1 and on < CDP_DELAY_S, timings
    # The focus helper marks itself with true, the tvos helper with its name.
    assert await content.evaluate("window.__tvRemoteFocus") == (True if direct else "tvos")


async def presses_arrived(frame, count):
    async with asyncio.timeout(2):
        while await frame.evaluate("window.__tvRemote?.count ?? 0") < count:
            await asyncio.sleep(0.01)


async def test_a_press_before_the_key_setup_still_gets_the_helper(remote, browser_frame, monkeypatch):
    """The show returns before its key setup runs. A press that comes first prepares the page itself."""
    service, observer, control = remote
    present_as(service, "mirror")
    frame = browser_frame
    control.interaction.binding = AsyncMock(return_value=(frame, frame.url, False))
    held, prime = asyncio.Event(), control.prime

    async def late_prime():
        await held.wait()
        await prime()

    monkeypatch.setattr(control, "prime", late_prime)
    result = await service.tools.call("tv_show", {"html": "<h1>x</h1>"})
    assert result["data"]["remote"] == {"mode": "tvos", "state": "starting"}
    assert await frame.evaluate("window.__tvRemoteFocus ?? null") is None
    observer.emit({"event": "button", "key": "right", "received": time.time()})
    await presses_arrived(frame, 1)
    assert await frame.evaluate("[document.activeElement.id, window.__tvRemoteFocus]") == ["a", "tvos"]
    # The setup that runs after it leaves the focus alone.
    held.set()
    await control.priming
    assert await frame.evaluate("[document.activeElement.id, window.__tvRemote.count]") == ["a", 1]


async def test_the_key_setup_and_a_press_at_once_share_one_cdp_session(remote, browser_frame):
    service, observer, control = remote
    present_as(service, "mirror")
    frame = browser_frame
    made = []

    async def new_cdp_session(_page):
        made.append(_page)
        return await frame.page.context.new_cdp_session(frame.page)

    page = SimpleNamespace(evaluate=frame.page.evaluate, context=SimpleNamespace(new_cdp_session=new_cdp_session))
    proxy = SimpleNamespace(page=page, evaluate=frame.evaluate, url=frame.url)
    control.interaction.binding = AsyncMock(return_value=(proxy, frame.url, False))
    await service.tools.call("tv_show", {"html": "<h1>x</h1>"})
    observer.emit({"event": "button", "key": "right", "received": time.time()})
    await presses_arrived(frame, 1)
    await control.priming
    assert len(made) == 1 and await frame.evaluate("document.activeElement.id") == "a" and control.missed == 0
