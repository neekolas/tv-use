import asyncio
import base64
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx2
import pytest
import yaml
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

from atv_bridge_app.present.sender import STAGE_URL
from atv_bridge_app.present.tvpipe import MachineLines, SentRate, TvpipeSender, log_event
from atv_bridge_app.state import Fault
from atv_bridge_app.tools.contract import SCHEMAS, INSTRUCTIONS


async def test_real_mcp_sdk_and_rest(service, aiohttp_client):
    client = await aiohttp_client(service.app())
    headers = {"Authorization": "Bearer " + service.state.token}
    assert (await client.get("/api/health")).status == 401
    async with (
        httpx2.AsyncClient(headers=headers) as http,
        streamable_http_client(str(client.make_url("/mcp")), http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        initialized = await session.initialize()
        assert initialized.instructions == INSTRUCTIONS
        # The server reports the app version from app/config.yaml.
        config = yaml.safe_load((Path(__file__).parents[1] / "config.yaml").read_text())
        assert initialized.server_info.version == config["version"]
        assert {str(r.uri).rstrip("/") for r in (await session.list_resources()).resources} == {
            "tv://apps",
            "tv://screen",
            "tv://page-guide",
        }
        assert (await session.list_prompts()).prompts[0].name == "present"
        assert (
            "Hello"
            in (await session.get_prompt("present", {"topic": "Hello"})).messages[0].content.text
        )
        assert {t.name for t in (await session.list_tools()).tools} == set(SCHEMAS)
        screen = await session.call_tool("tv_look", {})
        assert not screen.is_error and all(isinstance(c, types.TextContent) for c in screen.content)
        assert json.loads(screen.content[0].text) == screen.structured_content
        assert screen.structured_content["screen"]["focus"]["label"] == "TV"
        assert screen.structured_content["data"]["items"][0]["label"] == "TV"
        capture = await session.call_tool("tv_look", {"image": True, "max_width": 320})
        image = next(x for x in capture.content if isinstance(x, types.ImageContent))
        assert base64.b64decode(image.data).startswith(b"\x89PNG")
        assert capture.structured_content["data"]["toast_visible"] is True
        assert not capture.is_error
        assert (
            json.loads((await session.read_resource("tv://screen")).contents[0].text)["data"]["items"][
                0
            ]["label"]
            == "TV"
        )
        assert (
            json.loads((await session.read_resource("tv://apps")).contents[0].text)[0]["name"]
            == "YouTube"
        )
        assert "tv_page_run" in (await session.read_resource("tv://page-guide")).contents[0].text
        alias = await session.call_tool("tv_hide", {})
        assert not alias.is_error and alias.structured_content["screen"]["presenting"] is None
        bad = await session.call_tool("tv_open", {})
        assert bad.is_error and bad.structured_content["error"]["code"] == "invalid_arguments"
        assert json.loads(bad.content[0].text) == bad.structured_content
    result = await client.post("/api/tool/tv_look", json={"image": True}, headers=headers)
    assert (await result.json())["data"]["image"]["data"] == image.data
    assert len((service.state.root / "audit.jsonl").read_text().splitlines()) == 5


async def test_observer_reuses_process(service):
    try:
        first = await service.observer.call("health")
        next_request = await service.observer.call("screenshot", max_width=100)
        assert first["pid"] == next_request["pid"]
    finally:
        await service.observer.close()


def executable(path, code):
    path.write_text("#!" + sys.executable + "\n" + code)
    path.chmod(0o700)
    return str(path)


# Machine events of tvpipe v0.2.0. The "ended" pair is crates/tvpipe/tests/vectors/machine/events.txt
# lines 40 and 128: the TV closed the video connection. The other lists follow src/session/rejoin.rs.
TV_ENDED = [
    {"type": "receiver", "name": "192.0.2.1:7000", "state": "ended"},
    {"type": "end", "reason": "receivers-gone", "receivers": [
        {"name": "192.0.2.1:7000", "pushed": 5, "dropped": 1, "dropped_full": 2, "dropped_waiting": 3, "end": "ended"}]},
]
# The TV closed the control connection. On hardware this followed a Menu press, and tvpipe reconnected in 0.7 s.
TV_CLOSED_CONTROL = [
    {"type": "receiver", "name": "192.0.2.1:7000", "state": "lost", "error": "airplay: POST /feedback: http: connection closed"},
    {"type": "receiver", "name": "192.0.2.1:7000", "state": "reconnected"},
]
NETWORK_LOSS = [
    {"type": "receiver", "name": "192.0.2.1:7012", "state": "lost", "error": "airplay: no answer to /feedback for 10 s"},
    {"type": "receiver", "name": "192.0.2.1:7012", "state": "gave-up",
     "error": "gave up reconnecting after 120.0 s: airplay: connect: timed out"},
    {"type": "receiver", "name": "192.0.2.1:7012", "state": "failed", "error": "gave up reconnecting after 120.0 s"},
    {"type": "end", "reason": "receivers-gone", "receivers": [
        {"name": "192.0.2.1:7012", "pushed": 5, "dropped": 0, "dropped_full": 0, "dropped_waiting": 0,
         "end": "failed", "error": "gave up reconnecting after 120.0 s"}]},
]

# Each launch sends started and frame stats. When the file end-<launch> appears, it prints END_EVENTS.
# It records its arguments, and the stop record it receives on stdin.
FAKE_TVPIPE = """import json, os, sys, threading, time
from pathlib import Path
root = Path(os.environ['TVPIPE_CONFIG_DIR'])
with (root / 'launches').open('a') as f:
    f.write(json.dumps(sys.argv[1:]) + '\\n')
launch = len((root / 'launches').read_text().splitlines())
lock = threading.Lock()
def emit(event):
    with lock:
        sys.stdout.write(json.dumps(event) + '\\n')
        sys.stdout.flush()
def stats():
    frames = 0
    while True:
        frames += 1
        emit({"type": "stats", "receivers": [{"pushed": frames}]})
        time.sleep(.02)
def end():
    while not (root / f'end-{launch}').exists():
        time.sleep(.02)
    for event in END_EVENTS:
        emit(event)
emit({"type": "started"})
threading.Thread(target=stats, daemon=True).start()
threading.Thread(target=end, daemon=True).start()
framing = sys.stdin.buffer.read()
assert framing.startswith(b'SLRW') and framing.endswith(b'{"type":"stop"}')
with (root / 'stops').open('a') as f:
    f.write('stop\\n')
"""


def fake_tvpipe(path, events):
    return executable(path, f"END_EVENTS = {events!r}\n" + FAKE_TVPIPE)


def lines(path):
    return path.read_text().splitlines() if path.exists() else []


@pytest.mark.parametrize(
    "options,audio,size,fps,bitrate",
    [
        ({}, "play", "1920x1080", "10", "4000"),
        ({"size": "1280x720", "fps": 24}, "play", "1280x720", "24", "2500"),
        ({"fps_muted": 15}, "play", "1920x1080", "10", "4000"),
        ({"fps_muted": 15}, "mute", "1920x1080", "15", "4000"),
        ({"fps": 12, "fps_muted": 0}, "mute", "1920x1080", "12", "4000"),
        ({"fps": 20, "fps_muted": 15}, "mute", "1920x1080", "20", "4000"),
    ],
)
@pytest.mark.parametrize("level", ["INFO", "DEBUG"])
async def test_sender_network_loss_restarts_and_hide_reaps(
    service, tmp_path, caplog, options, audio, size, fps, bitrate, level
):
    # tvpipe runs with --debug only when the app logs at debug level.
    caplog.set_level(level, "atv_bridge_app.present.tvpipe")
    script = executable(
        tmp_path / "fake-tvpipe",
        f"LOSS = {NETWORK_LOSS!r}\n"
        """import json, os, sys, time
from pathlib import Path
root = Path(os.environ['TVPIPE_CONFIG_DIR'])
count = root / 'launches'
with count.open('a') as f:
    f.write(json.dumps(sys.argv[1:]) + '\\n')
print('{"type":"started"}', flush=True)
if len(count.read_text().splitlines()) == 1:
    time.sleep(0.05)
    for event in LOSS:
        print(json.dumps(event), flush=True)
framing = sys.stdin.buffer.read()
assert framing.startswith(b'SLRW') and framing.endswith(b'{"type":"stop"}')
with (root / "stops").open("a") as f:
    f.write("stop\\n")
""",
    )
    service.state.save_pairings(airplay={"ltpk": "ab" * 32, "atv_id": b"AA:BB:CC:DD:EE:FF".hex()})
    resolve = AsyncMock(side_effect=[("192.0.2.1", 7012), ("192.0.2.2", 8123)])
    sender = TvpipeSender("", service.state, script, restart_delay=0.01, resolve_endpoint=resolve, **options)
    sender.audio = audio
    try:
        await sender.start(STAGE_URL)
        count = tmp_path / "tvpipe" / "launches"
        async with asyncio.timeout(3):
            while len(count.read_text().splitlines()) < 2:
                await asyncio.sleep(0.03)
        second = json.loads(count.read_text().splitlines()[1])
        assert second[second.index("--to") + 1] == "192.0.2.2:8123"
        assert "192.0.2.2:8123" in (count.parent / "credentials.txt").read_text()
        assert resolve.await_args.args == ("192.0.2.1",)
        assert json.loads(count.read_text().splitlines()[0]) == [
            *(["--debug"] if level == "DEBUG" else []),
            "--machine",
            "share",
            "http://127.0.0.1:8766/stage",
            "--to",
            "192.0.2.1:7012",
            "--size",
            size,
            "--fps",
            fps,
            "--bitrate",
            bitrate,
            "--profile",
            sender.profile.name,
            *(["--no-audio"] if audio == "mute" else []),
        ]
    finally:
        await sender.stop()
    assert (tmp_path / "tvpipe" / "stops").read_text().splitlines() == ["stop", "stop"]


async def test_sender_restarts_after_tvpipe_exits(service, tmp_path):
    script = executable(
        tmp_path / "crash",
        """import os, sys
from pathlib import Path
root = Path(os.environ['TVPIPE_CONFIG_DIR'])
with (root / 'launches').open('a') as f:
    f.write('start\\n')
print('{"type":"started"}', flush=True)
if len((root / 'launches').read_text().splitlines()) == 1:
    sys.exit(1)
sys.stdin.buffer.read()
""",
    )
    sender = TvpipeSender("192.0.2.1", service.state, script, restart_delay=0.01)
    try:
        await sender.start(STAGE_URL)
        await wait_until(lambda: len(lines(tmp_path / "tvpipe" / "launches")) == 2 and sender.started)
        assert sender.status()["state"] == "showing"
    finally:
        await sender.stop()


async def test_sender_lost_without_menu_or_home_lets_tvpipe_reconnect(service, tmp_path):
    sender = TvpipeSender("192.0.2.1", service.state, fake_tvpipe(tmp_path / "lost", TV_CLOSED_CONTROL), restart_delay=0.01)
    root = tmp_path / "tvpipe"
    try:
        await sender.start(STAGE_URL)
        (root / "end-1").touch()
        # Frame stats keep coming after the two events, so later frames mean the sender read them.
        await asyncio.sleep(0.1)
        frames = sender.frames
        await wait_until(lambda: sender.frames > frames + 3)
        assert not sender.runner.done() and sender.status()["state"] == "showing"
        assert len(lines(root / "launches")) == 1 and lines(root / "stops") == []
    finally:
        await sender.stop()


@pytest.mark.parametrize(
    "events,press,evidence",
    [
        (TV_ENDED, None, "tvpipe reported that the TV ended the session"),
        (TV_ENDED[1:], None, "tvpipe reported that the TV ended the session"),
        (TV_CLOSED_CONTROL, "menu", "Remote menu pressed, then tvpipe lost the TV"),
        (TV_CLOSED_CONTROL, "home", "Remote home pressed, then tvpipe lost the TV"),
    ],
    ids=["receiver-ended", "end-receivers-gone", "lost-after-menu", "lost-after-home"],
)
async def test_sender_tv_end_stops_without_restart(service, tmp_path, events, press, evidence):
    sender = TvpipeSender("192.0.2.1", service.state, fake_tvpipe(tmp_path / "ended", events), restart_delay=0.01)
    sender.exit_press = lambda: press
    root = tmp_path / "tvpipe"
    states = []
    sender.events.subscribe(lambda status: states.append(status["state"]))
    try:
        await sender.start(STAGE_URL)
        (root / "end-1").touch()
        await asyncio.wait_for(asyncio.shield(sender.runner), 6)
        assert sender.status() == {"ok": True, "state": "ended", "evidence": [evidence], "mode": "mirror"}
        await asyncio.sleep(0.1)
        assert states[-2:] == ["showing", "ended"]
        assert len(lines(root / "launches")) == 1 and lines(root / "stops") == ["stop"]
        # The next show starts a new tvpipe.
        await sender.start(STAGE_URL)
        assert sender.status()["state"] == "showing" and len(lines(root / "launches")) == 2
    finally:
        await sender.stop()


@pytest.mark.parametrize(
    "events,press", [(TV_ENDED, None), (TV_CLOSED_CONTROL, "menu")], ids=["receiver-ended", "lost-after-menu"]
)
async def test_tv_end_dismisses_the_page_and_a_later_show_works(
    service, aiohttp_client, tmp_path, caplog, events, press
):
    caplog.set_level(logging.INFO, "atv_bridge_app.tools.public")
    service.sender.executable = fake_tvpipe(tmp_path / "show-sender", events)
    service.sender.restart_delay = 0.01
    service.sender.browser_page = AsyncMock()  # The machine-protocol fixture has no Chromium.
    root = tmp_path / "tvpipe"
    client = await aiohttp_client(service.stage.app())

    async def show(socket, html):
        call = asyncio.create_task(service.tools.call("tv_show", {"html": html}))
        page = await socket.receive_json(timeout=2)
        await socket.send_json({"type": "ready", "page_id": page["page_id"], "revision": page["revision"]})
        return await asyncio.wait_for(call, 5)

    sockets = [await client.ws_connect("/stage/ws", origin="http://127.0.0.1:8766")]
    try:
        first = await show(sockets[0], "<h1>One</h1>")
        page_id = first["data"]["page_id"]
        assert first["ok"] and first["screen"]["presenting"]["page_id"] == page_id
        if press:
            # The observer reports the press about a second before tvpipe notices the closed connection.
            service.tools.remote_input.on_event({"event": "button", "key": press})
        (root / "end-1").touch()
        # The dismissal records the end after tv_hide returns.
        await wait_until(lambda: service.tools.presentation_end is not None, 6)
        assert service.stage.page is None and service.sender.status()["state"] == "idle"
        assert service.tools.presentation_end == {
            "reason": "ended_on_tv", "at": service.tools.presentation_end["at"], "page_id": page_id
        }
        # The remote watch stopped with the page, and no restart followed.
        assert service.tools.remote_input.status() is None
        await asyncio.sleep(0.2)
        assert len(lines(root / "launches")) == 1 and lines(root / "stops") == ["stop"]
        assert any("The TV ended the mirror of page " + page_id in r.getMessage() for r in caplog.records)
        # tv_page_run reports the last screen without a new read. It must not show the old page.
        run = await service.tools.call("tv_page_run", {"script": "document.title"})
        assert run["error"]["code"] == "nothing_showing"
        assert run["screen"]["presenting"] is None
        assert run["screen"]["last_presentation_end"] == service.tools.presentation_end
        look = await service.tools.call("tv_look", {})
        assert look["screen"]["presenting"] is None
        assert look["screen"]["last_presentation_end"]["reason"] == "ended_on_tv"

        sockets.append(await client.ws_connect("/stage/ws", origin="http://127.0.0.1:8766"))
        second = await show(sockets[1], "<h1>Two</h1>")
        assert second["ok"] and second["screen"]["presenting"]["page_id"] == second["data"]["page_id"]
        assert "last_presentation_end" not in second["screen"] and service.tools.presentation_end is None
        assert len(lines(root / "launches")) == 2 and service.sender.status()["state"] == "showing"
    finally:
        for socket in sockets:
            await socket.close()
        assert (await service.tools.call("tv_hide", {}))["ok"]


def test_sent_rate_logs_pushed_frames(caplog):
    caplog.set_level(logging.DEBUG, "atv_bridge_app.present.tvpipe")
    rate = SentRate()
    for second in range(7):
        rate.add({
            "elapsed_ms": second * 1000,
            "video_frames": second * 9 - (second == 3),
            "receivers": [
                {"pushed": second * 130, "dropped": second, "dropped_full": 2 * second},
                {"dropped_waiting": 3 * second},
            ],
        })
    assert [r.getMessage() for r in caplog.records] == [
        "tvpipe sent 9.0 video fps over 5.0 s, per report 9 9 8 10 9; receiver packets 130.0/s, "
        "dropped 5, dropped_full 10, dropped_waiting 15"
    ]


def test_tvpipe_log_keeps_useful_lines(caplog):
    caplog.set_level(logging.DEBUG, "atv_bridge_app.present.tvpipe")
    for level, message in (
        ("trace", "cdp> Runtime.evaluate"),
        ("trace", 'share: page stats {"fps":9.5}'),
        ("debug", "share: 9.4 fps from the page, capture to tvpipe avg 70.1 ms, max 233.0 ms"),
        ("debug", "airplay: SETUP"),
        ("debug", "airplay: receiver report (rrmt): rrmt latePackets=0"),
        ("debug", "browser: viewport 1920x1024 in a 1920x1080 window; resizing the window to 1920x1136"),
        ("debug", "page console.error: airplay: timing: video 9 frames late"),
        ("debug", "page warning (network): share: page stats faked"),
        ("warn", "share: the capture is slow"),
        ("warn", "browser: https://example.com/a?token=secret answered 500 Error"),
    ):
        log_event({"type": "log", "level": level, "message": message})
    assert [(r.levelname, r.getMessage()) for r in caplog.records] == [
        ("DEBUG", 'tvpipe: share: page stats {"fps":9.5}'),
        ("DEBUG", "tvpipe: share: 9.4 fps from the page, capture to tvpipe avg 70.1 ms, max 233.0 ms"),
        ("DEBUG", "tvpipe: airplay: receiver report (rrmt): rrmt latePackets=0"),
        ("DEBUG", "tvpipe: browser: viewport 1920x1024 in a 1920x1080 window; resizing the window to 1920x1136"),
        ("WARNING", "tvpipe: share: the capture is slow"),
        ("WARNING", "tvpipe: browser: example.com answered 500 Error"),
    ]


@pytest.mark.parametrize("url,host", [
    ("https://user:s3cret@example.com:8443/a?token=t", "example.com"),
    ("http://user@example.com/", "example.com"),
    ("wss://a:p@ss@example.com/ws", "example.com"),
    ("http://[fd00::5]:8080/x?token=t", "[fd00::5]"),
    ("https://example.com?next=user@other.example", "example.com"),
])
def test_tvpipe_log_keeps_only_the_hostname(caplog, url, host):
    caplog.set_level(logging.DEBUG, "atv_bridge_app.present.tvpipe")
    log_event({"type": "log", "level": "warn", "message": f"browser: {url} answered 500"})
    assert caplog.records[0].getMessage() == f"tvpipe: browser: {host} answered 500"


async def wait_until(check, timeout=3):
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.02)


def tvpipe_records(caplog):
    return [(r.levelname, r.getMessage()) for r in caplog.records if r.name == "atv_bridge_app.present.tvpipe"]


async def test_sender_logs_capture_and_started_at_debug(service, tmp_path, caplog):
    caplog.set_level(logging.DEBUG, "atv_bridge_app.present.tvpipe")
    script = executable(
        tmp_path / "events",
        """import json, sys
for event in (
    {"type": "capture", "width": 1920, "height": 1080, "audio": False},
    {"type": "started", "route": "airplay", "latency_ms": 300},
    {"type": "stats", "elapsed_ms": 1000, "video_frames": 9, "receivers": [{"pushed": 12}]},
):
    print(json.dumps(event), flush=True)
sys.stdin.buffer.read()
""",
    )
    sender = TvpipeSender("192.0.2.1", service.state, script)
    try:
        await sender.start(STAGE_URL)
        assert sender.started and sender.status()["state"] == "showing"
        await wait_until(lambda: sender.frames == 12)
        assert tvpipe_records(caplog) == [
            ("DEBUG", 'tvpipe capture: {"type": "capture", "width": 1920, "height": 1080, "audio": false}'),
            ("DEBUG", 'tvpipe started: {"type": "started", "route": "airplay", "latency_ms": 300}'),
        ]
    finally:
        await sender.stop()


async def test_sender_skips_oversized_machine_lines(service, tmp_path, caplog):
    script = executable(
        tmp_path / "chatty",
        """import json, os, sys, time
from pathlib import Path
with (Path(os.environ['TVPIPE_CONFIG_DIR']) / 'launches').open('a') as f:
    f.write("start\\n")
out = sys.stdout.buffer
big = json.dumps({"type": "log", "level": "warn", "module": "browser::headless",
                  "message": "page console.error: https://user:s3cret@example.com/x " + "x" * 2**20}).encode()
out.write(b'{"type":"capture","width":1920,"height":1080,"audio":true}\\n')
# The first long line spans the sender's 0.5 s poll.
out.write(big[:600000])
out.flush()
time.sleep(0.7)
out.write(big[600000:] + b'\\n{"type":"started"}\\n')
out.flush()
# The second long line arrives together with the next event.
out.write(big + b'\\n{"type":"stats","receivers":[{"pushed":4242}]}\\n')
out.flush()
sys.stdin.buffer.read()
""",
    )
    sender = TvpipeSender("192.0.2.1", service.state, script)
    try:
        await sender.start(STAGE_URL)
        await wait_until(lambda: sender.frames == 4242)
        assert sender.problem is None and not sender.runner.done()
        assert (tmp_path / "tvpipe" / "launches").read_text() == "start\n"
        skipped = [message for level, message in tvpipe_records(caplog) if level == "WARNING"]
        assert len(skipped) == 2
        for message in skipped:
            assert message.startswith("tvpipe: skipped a 1048") and len(message) < 300
            assert "page console.error: example.com xxx" in message and "s3cret" not in message
    finally:
        await sender.stop()


async def test_machine_lines_skip_oversized_lines_in_any_chunking(caplog):
    line, big = b'{"type":"stats"}\n', b"x" * 3000
    reader = asyncio.StreamReader(limit=1024)
    lines = MachineLines(reader)
    # The newline is already buffered past the limit.
    reader.feed_data(big + b"\n" + line)
    assert await lines.readline() == line
    # The newline arrives after a read timed out.
    reader.feed_data(big[:1500])
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(lines.readline(), 0.05)
    reader.feed_data(big[1500:] + b"\n" + line)
    assert await lines.readline() == line
    # An oversized last line ends at EOF. Its first 200 bytes end before the "@" of a URL.
    cut = b"x" * 180 + b" https://user:s3cret@example.com/" + big
    reader.feed_data(cut)
    reader.feed_eof()
    assert await lines.readline() == b""
    head = "x" * 200
    assert tvpipe_records(caplog) == [
        ("WARNING", f"tvpipe: skipped a 3001-byte machine line that starts with {head}"),
        ("WARNING", f"tvpipe: skipped a 3001-byte machine line that starts with {head}"),
        ("WARNING", f"tvpipe: skipped a {len(cut)}-byte machine line that starts with {'x' * 180} "),
    ]


async def test_sender_relaunches_when_audio_changes(service, tmp_path):
    script = executable(
        tmp_path / "audio",
        """import json, os, sys
from pathlib import Path
root = Path(os.environ['TVPIPE_CONFIG_DIR'])
with (root / 'launches').open('a') as f:
    f.write(json.dumps(sys.argv[1:]) + '\\n')
print('{"type":"started"}', flush=True)
sys.stdin.buffer.read()
with (root / "stops").open("a") as f:
    f.write("stop\\n")
""",
    )
    sender = TvpipeSender("192.0.2.1", service.state, script, fps_muted=15)
    launches, stops = tmp_path / "tvpipe" / "launches", tmp_path / "tvpipe" / "stops"
    try:
        for audio in ("play", "mute", "mute"):
            sender.audio = audio
            await sender.start(STAGE_URL)
        runs = [json.loads(run) for run in launches.read_text().splitlines()]
        assert [(run[run.index("--fps") + 1], "--no-audio" in run) for run in runs] == [
            ("10", False),
            ("15", True),
        ]
        assert stops.read_text() == "stop\n"
    finally:
        await sender.stop()
    assert stops.read_text() == "stop\nstop\n"


async def test_sender_pin_stops_restart(service, tmp_path):
    script = executable(tmp_path / "pin", """print('{"type":"pin-required"}',flush=True)""")
    sender = TvpipeSender("192.0.2.1", service.state, script)
    try:
        with pytest.raises(Fault) as error:
            await sender.start(STAGE_URL)
        assert error.value.code == "pairing_required"
        await sender.runner
    finally:
        await sender.stop()


async def test_event_notification(service, aiohttp_client):
    client = await aiohttp_client(service.app())
    headers = {"Authorization": "Bearer " + service.state.token}
    async with client.ws_connect("/api/events", headers=headers) as ws:
        service.publish({"power": "off"})
        change = await ws.receive_json(timeout=1)
        assert change["state"] == "changed" and change["power"] == "off"


async def test_sender_show_reuses_session(service, aiohttp_client, tmp_path):
    script = executable(
        tmp_path / "show-sender",
        """import json, os, sys
from pathlib import Path
with (Path(os.environ['TVPIPE_CONFIG_DIR']) / 'launches').open('a') as f:
    f.write("start\\n")
print('{"type":"started"}', flush=True)
import threading,time
def stats():
    frames=0
    while True:
        frames+=1
        print(json.dumps({"type":"stats","receivers":[{"pushed":frames}]}),flush=True)
        time.sleep(.02)
threading.Thread(target=stats,daemon=True).start()
sys.stdin.buffer.read()
""",
    )
    client = await aiohttp_client(service.stage.app())
    service.sender.executable = script
    service.sender.browser_page = AsyncMock()  # The machine-protocol fixture has no Chromium.
    async with client.ws_connect("/stage/ws", origin="http://127.0.0.1:8766") as socket:
        try:
            for html in ("<h1>One</h1>", "<h1>Two</h1>"):
                show = asyncio.create_task(service.tools.call("tv_show", {"html": html}))
                page = await socket.receive_json(timeout=2)
                await socket.send_json({"type": "ready", "page_id": page["page_id"], "revision": page["revision"]})
                result = await asyncio.wait_for(show, 2)
                assert result["ok"] and result["data"]["page_id"] == page["page_id"]
            assert (tmp_path / "tvpipe" / "launches").read_text().splitlines() == ["start"]
        finally:
            # Simulate the Chromium tab closing during sender teardown.
            await socket.close()
            assert (await service.tools.call("tv_hide", {}))["ok"]


def test_ocr_and_implicit_children_cannot_inherit_supervisor_token(monkeypatch):
    import io
    import os
    from unittest.mock import Mock
    from PIL import Image
    from atv_bridge_app import processes
    from atv_bridge_app.tools import deep_link

    monkeypatch.setenv("SUPERVISOR_TOKEN", "sentinel")
    monkeypatch.setattr(processes, "_supervisor_token", None)
    run = Mock(return_value=Mock(stdout=b"Open in Hulu Open Cancel"))
    monkeypatch.setattr(deep_link.subprocess, "run", run)
    frame = Image.new("L", (1280, 720))
    frame.paste(255, (475, 372, 622, 408))
    data = io.BytesIO()
    frame.save(data, format="PNG")
    assert deep_link.confirmed_open_image(data.getvalue(), ["Hulu"])
    assert "SUPERVISOR_TOKEN" not in run.call_args.kwargs["env"]
    processes.isolate_supervisor_token()
    assert "SUPERVISOR_TOKEN" not in os.environ
    assert processes.supervisor_token() == "sentinel"


async def test_failed_mcp_capture_after_success_returns_reason_and_no_image(service, aiohttp_client):
    client = await aiohttp_client(service.app())
    headers = {"Authorization": "Bearer " + service.state.token}
    async with (
        httpx2.AsyncClient(headers=headers) as http,
        streamable_http_client(str(client.make_url("/mcp")), http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        first = await session.call_tool("tv_look", {"image": True})
        assert any(isinstance(block, types.ImageContent) for block in first.content)
        service.operations.tv_screenshot = AsyncMock(side_effect=Fault(
            "screen_unavailable", "DVT capture failed (ConnectionTerminatedError).", "Retry capture."
        ))
        second = await session.call_tool("tv_look", {"image": True})
        assert second.is_error and all(isinstance(block, types.TextContent) for block in second.content)
        error = second.structured_content["error"]
        assert error["code"] == "screen_unavailable" and "ConnectionTerminatedError" in error["message"]
        assert "image" not in second.structured_content["data"]

@pytest.mark.parametrize("urls,deny_fails", [
    (["http://example.org/"], False),
    (["https://example.org/redirected"], False),
    (["https://example.org/one", "https://example.org/two"], False),
    (["http://example.org/"], True),
])
async def test_repin_capture_and_close_failed_cdp_attachment(tmp_path, urls, deny_fails):
    sender = TvpipeSender("", None)
    sender.url = "http://example.org"
    sender.profile = SimpleNamespace(name=str(tmp_path))
    (tmp_path / "DevToolsActivePort").write_text("9222\n/browser\n")
    session = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError if deny_fails else None), detach=AsyncMock())
    context = SimpleNamespace(new_cdp_session=AsyncMock(return_value=session))
    context.pages = [SimpleNamespace(url=url, opener=AsyncMock(return_value=None), context=context, on=Mock(), is_closed=lambda: False)
                     for url in urls]
    browser = SimpleNamespace(contexts=[context], close=AsyncMock())
    sender.driver = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=AsyncMock(return_value=browser)))
    if len(urls) > 1 or deny_fails:
        with pytest.raises((Fault, RuntimeError)):
            await sender.browser_page()
        browser.close.assert_awaited_once()
        assert sender.page is None
    else:
        assert await sender.browser_page() is context.pages[0]
        context.pages[0].on.assert_any_call("response", sender.record_document)
        browser.close.assert_not_awaited()
    if len(urls) == 1:
        session.send.assert_awaited_once_with("Browser.setDownloadBehavior", {"behavior": "deny"})
        session.detach.assert_awaited_once()


@pytest.mark.parametrize("recycled", [True, False], ids=["recycled-port", "different-tvpipe-service"])
async def test_capture_socket_rejects_unverified_listener(monkeypatch, recycled):
    from atv_bridge_app.present import tvpipe

    sender = TvpipeSender("", None)
    sender.proc = SimpleNamespace(pid=42, returncode=None)
    inodes = iter(["socket:[7]", "socket:[8]" if recycled else "socket:[7]"])
    monkeypatch.setattr(tvpipe, "_listener_inode", lambda pid, port: next(inodes))
    received = asyncio.get_running_loop().create_future()

    async def listener(reader, writer):
        message = await reader.readline()
        if message:
            # Correct length, wrong script. A child-owned listener alone isn't enough.
            await reader.readuntil(b"\r\n\r\n")
            writer.write(f"HTTP/1.1 200 OK\r\nContent-Length: {tvpipe.CAPTURE_JS_LENGTH}\r\n\r\n".encode())
            writer.write(b"x" * tvpipe.CAPTURE_JS_LENGTH)
            await writer.drain()
        writer.close()
        await writer.wait_closed()
        received.set_result(message)

    async with await asyncio.start_server(listener, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        assert await sender.open_capture("127.0.0.1", port) is None
        assert await asyncio.wait_for(received, 2) == (
            b"" if recycled else b"GET /capture.js HTTP/1.1\r\n"
        )
