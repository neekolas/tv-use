import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientSession

from atv_bridge_app.present.play_url import PlayUrlSender
from atv_bridge_app.present.sender import STAGE_URL, AutoSender, Sender
from atv_bridge_app.state import Fault


class ProbeSender(Sender):
    def __init__(self, mode, failures=0):
        super().__init__(mode)
        self.calls = []
        self.failures = failures

    async def start(self, stage_url):
        self.calls.append(stage_url)
        if self.failures:
            self.failures -= 1
            raise Fault("sender_failed", "Start failed.", "Try again.", True)
        self.transition("showing")

    async def stop(self):
        self.calls.append("stop")
        self.transition("idle")


@pytest.mark.parametrize(
    "failures,mode,count", [(0, "mirror", 1), (1, "mirror", 2), (2, "play_url", 2)]
)
async def test_auto_switches_after_two_failed_starts(failures, mode, count):
    mirror, clip = ProbeSender("mirror", failures), ProbeSender("play_url")
    sender = AutoSender(mirror, clip)
    events = []
    sender.events.subscribe(events.append)
    await sender.start(STAGE_URL)
    assert mirror.calls.count(STAGE_URL) == count
    assert events[-1] == sender.status() and events[-1]["mode"] == mode
    if mode == "play_url":
        assert mirror.calls == [STAGE_URL, "stop"] * 2
        await sender.start(STAGE_URL)
        assert clip.calls == [STAGE_URL] * 2
    await sender.stop()
    assert sender.status()["state"] == "idle"
    await sender.start(STAGE_URL)
    assert sender.status()["mode"] == "mirror"


async def test_mp4_reopens_on_cut_and_removes_media(service, aiohttp_server):
    stage, engine = service.stage, service.engine
    engine.adapter.host = "127.0.0.1"
    engine.adapter.supports_play_url = True
    service.state.save_pairings(airplay=True)
    server = await aiohttp_server(stage.app())

    async def render(work):
        path = work / "stage.mp4"
        path.write_bytes(b"fake mp4")
        return path, 60

    renderer = SimpleNamespace(open=AsyncMock(), render=render, close=AsyncMock())
    sender = PlayUrlSender(engine, stage, renderer)
    client = await engine.ensure()
    started = asyncio.Event()
    calls = []
    async with ClientSession() as http:

        async def play(url, position):
            calls.append((url, position))
            async with http.get(url, headers={"Range": "bytes=0-3"}) as response:
                assert response.status == 206 and await response.read() == b"fake"
            if len(calls) == 1:
                await asyncio.sleep(1.05)
                raise ConnectionResetError("receiver ended session")
            started.set()
            await asyncio.Future()

        connect = engine.adapter.connect

        async def reconnect(*args, **kwargs):
            fresh = await connect(*args, **kwargs)
            fresh.play_url = play
            return fresh

        engine.adapter.connect = reconnect
        client.play_url = play
        states = []
        sender.events.subscribe(lambda result: states.append(result["state"]))
        try:
            await sender.start(str(server.make_url("/stage")))
            assert sender.status()["state"] == "rendering"
            await asyncio.wait_for(started.wait(), 6)
            assert calls[0][0].split("?")[0] == calls[1][0].split("?")[0]
            assert calls[0][0] != calls[1][0] and calls[0][1] == 0 and calls[1][1] >= 1
            assert "showing" in states and "reconnecting" in states
        finally:
            await sender.stop()
    assert not list(stage.media.glob("*.mp4")) and not stage.media_reads
    assert sender.status()["state"] == "idle" and renderer.close.await_count == 1


async def test_play_url_without_fork_is_actionable(service):
    service.engine.adapter.supports_play_url = False
    service.operations.sender = PlayUrlSender(service.engine, service.stage)
    result = await service.tools.call("tv_show", {"html": "Hi"})
    assert result["error"]["code"] == "launch_refused"
    assert service.stage.page is None
    assert (await service.operations.tv_health())["sender"]["error"]["code"] == "fork_required"


async def test_receiver_denial_is_terminal(service):
    service.engine.adapter.host = "127.0.0.1"
    service.engine.adapter.supports_play_url = True
    service.state.save_pairings(airplay=True)

    async def render(work):
        output = Path(work, "clip")
        output.write_bytes(b"fake mp4")
        return output, 60

    client = await service.engine.ensure()
    client.play_url = AsyncMock(side_effect=RuntimeError("403 Forbidden"))
    renderer = SimpleNamespace(open=AsyncMock(), render=render, close=AsyncMock())
    sender = PlayUrlSender(service.engine, service.stage, renderer)
    await sender.start(STAGE_URL)
    await asyncio.wait_for(sender.job, 2)
    assert sender.status()["error"]["code"] == "allow_access_changed"
    assert client.play_url.await_count == 1 and not list(service.stage.media.iterdir())


@pytest.mark.parametrize("arg", ["url", "video_id"])
@pytest.mark.parametrize("value", ["dQw4w9WgXcQ", "https://youtu.be/dQw4w9WgXcQ"])
async def test_integration_youtube_argument_forms(service, arg, value):
    assert (await service.operations.tv_play_youtube(**{arg: value}))["ok"]


async def test_integration_pairings_and_classification(service):
    service.state.save_pairings(airplay=True, companion=True, raop=True, remote_pairing=True)
    result = await service.operations.tv_health()
    assert result["pairings"] == {"airplay": True, "companion": True, "remote_pairing": True}
    assert result["pairing_needed"] == [] and result["classification"] == result["state"]
    service.engine.adapter.connect = AsyncMock(side_effect=ConnectionError())
    assert (await service.engine.status())["volume_supported"] is False
    assert (await service.operations.tv_health())["classification"] == "unreachable"
    assert not (await service.tools.call("tv_open", {}))["ok"]


async def test_show_replaces_pending_render_and_hide_cancels(service, aiohttp_client):
    stage = service.stage
    engine = service.engine
    engine.adapter.supports_play_url = True
    service.state.save_pairings(airplay=True)
    renders = asyncio.Queue()
    finished = []

    async def render(work):
        await renders.put(work)
        try:
            await asyncio.Future()
        finally:
            finished.append(work)

    renderer = SimpleNamespace(open=AsyncMock(), render=render, close=AsyncMock())
    sender = PlayUrlSender(engine, stage, renderer)
    service.operations.sender = sender
    client = await aiohttp_client(stage.app())
    async with client.ws_connect("/stage/ws", origin="http://127.0.0.1:8766") as ws:
        try:
            show = asyncio.create_task(service.tools.call("tv_show", {"html": "Hi"}))
            page = await ws.receive_json(timeout=2)
            await ws.send_json({"type": "ready", "page_id": page["page_id"], "revision": page["revision"]})
            assert (await show)["verified"] is False
            await asyncio.wait_for(stage.ready.wait(), 2)
            original = await asyncio.wait_for(renders.get(), 2)
            replacement = asyncio.create_task(service.tools.call("tv_show", {"html": "Replacement"}))
            page = await ws.receive_json(timeout=2)
            await ws.send_json({"type": "ready", "page_id": page["page_id"], "revision": page["revision"]})
            assert (await replacement)["ok"]
            updated = await asyncio.wait_for(renders.get(), 2)
            assert finished == [original] and not original.exists()
        finally:
            await ws.close()
            await service.operations.tv_hide()
    assert finished == [original, updated] and not updated.exists()
    assert sender.status()["state"] == "idle"


async def test_only_successful_media_get_confirms_playback(service, aiohttp_client):
    stage = service.stage
    (stage.media / "stage.mp4").write_bytes(b"clip")
    event = stage.media_reads["stage.mp4", "127.0.0.1", "current"] = asyncio.Event()
    resource = "/m/stage.mp4?session=current"
    client = await aiohttp_client(stage.app())
    async with client.get("/m/stage.mp4?session=previous") as stale:
        assert stale.status == 200 and not event.is_set()
    async with client.head(resource) as head:
        assert head.status == 200 and not event.is_set()
    async with client.get(resource, headers={"Range": "bytes=9-10"}) as bad_range:
        assert bad_range.status == 416 and not event.is_set()
    async with client.get(resource, headers={"Range": "bytes=1-2"}) as part:
        assert part.status == 206 and await part.read() == b"li" and event.is_set()


async def test_auto_does_not_fall_back_when_the_tv_ends_the_mirror():
    mirror, clip = ProbeSender("mirror"), ProbeSender("play_url")
    sender = AutoSender(mirror, clip)
    await sender.start(STAGE_URL)
    mirror.transition("ended", ["tvpipe reported that the TV ended the session"])
    assert sender.recovery is None and clip.calls == []
    assert sender.status()["state"] == "ended" and sender.status()["mode"] == "mirror"
    await sender.stop()
    assert sender.status()["state"] == "idle"


async def test_auto_handles_failed_receiver_restart_while_page_is_shown():
    mirror, clip = ProbeSender("mirror"), ProbeSender("play_url")
    sender = AutoSender(mirror, clip)
    await sender.start(STAGE_URL)
    mirror.failures = 1
    mirror.fail(Fault("sender_failed", "Receiver restart failed.", "Try again.", True))
    await asyncio.wait_for(sender.recovery, 1)
    assert mirror.calls == [STAGE_URL, "stop", STAGE_URL, "stop"]
    assert clip.calls == [STAGE_URL] and sender.status()["mode"] == "play_url"
    await sender.stop()
