import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from atv_bridge_app.atv import Session, check_reply, credentials
from atv_bridge_app.state import Fault, State
from atv_bridge_app.tools import link_info


async def test_wake_retries_with_new_connection(service):
    adapter = service.engine.adapter
    await service.engine.ensure()
    adapter.power_sequence.extend(["off", "on"])
    result = await service.engine.wake()
    assert result["ok"] and len(adapter.clients) == 4
    assert [tv.keys for tv in adapter.clients] == [[], ["wake"], ["wake"], []]
    assert [tv.closed for tv in adapter.clients] == [True, True, True, False]


async def test_wake_reports_failure_after_two_attempts(service):
    service.engine.adapter.power_sequence.extend(["off", "unknown"])
    result = await service.engine.wake()
    assert not result["ok"] and result["error"]["code"] == "wake_failed"
    assert len(result["evidence"]) == 4
    assert all(c.closed for c in service.engine.adapter.clients)


async def test_off_on_rebuilds_before_next_command(service):
    previous = await service.engine.ensure()
    service.engine.adapter.callback("off", "on")
    result = await service.operations.tv_sleep()
    assert previous.closed and not previous.keys
    assert result["ok"] and service.engine.client.keys == ["sleep"]


async def test_no_replay_after_uncertain_key(service):
    client = await service.engine.ensure()
    client.lose_key = "select"
    result = await service.operations.tv_press(["up", "select", "home"])
    assert result["delivered"] == "uncertain" and result["confirmed_keys"] == 1
    assert not result["error"]["retryable"] and client.closed
    assert client.keys == ["up", "select"] and len(service.engine.adapter.clients) == 1


async def test_youtube_verification_tries_second_form(service):
    result = await service.operations.tv_play_youtube(url="https://youtu.be/dQw4w9WgXcQ")
    assert result["state"] == "playing"
    assert service.engine.client.targets == [
        "youtube://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "youtube://watch/dQw4w9WgXcQ",
    ]


async def test_open_requires_evidence_and_masks_audit(service):
    client = await service.engine.ensure()
    client.open = AsyncMock()
    assert not (await service.tools.call("tv_open", {"app": "YouTube"}))["ok"]
    assert (await service.tools.call("tv_remote", {"text": "private phrase"}))["ok"]
    log = (service.state.root / "audit.jsonl").read_text()
    assert "private phrase" not in log and len(log.splitlines()) == 2
    assert link_info("https://tv.apple.com/movie/x?l=en")[0].endswith("?l=en&action=play")
    assert "reliability" in link_info("https://www.netflix.com/title/1")[1]


async def test_backoff_and_pairing_issue(service):
    adapter = service.engine.adapter
    adapter.connect = AsyncMock(side_effect=ConnectionError())
    await service.engine.status()
    await service.engine.status()
    assert adapter.connect.await_count == 1
    assert service.engine.health()["error"]["code"] == "unreachable"


@pytest.mark.parametrize("key", ["_em", "_ec", "_eu"])
def test_companion_error_fields(key):
    with pytest.raises(Exception, match="rejected"):
        check_reply({key: 14})


async def test_pair_reuse_fallback_and_identity(service):
    adapter = service.engine.adapter
    original = service.state.identity
    record = {"atv_id": "4142", "ltpk": "0f" * 32}
    handler = SimpleNamespace(close=AsyncMock())
    adapter.begin_pair = AsyncMock(return_value=handler)
    adapter.finish_pair = AsyncMock(return_value=record)
    adapter.verify_reuse = AsyncMock(side_effect=[RuntimeError("HTTP 401"), None])
    pairing = service.pairing
    session = await pairing.start("airplay")
    result = await pairing.finish(session["pairing_id"], "0259")
    assert result["credential_reuse"] == {"companion": False, "raop": True}
    assert "companion" in result["pairing_needed"]
    session = await pairing.start("companion")
    assert (await pairing.finish(session["pairing_id"], "0163"))["ok"]
    reopened = State(service.state.root)
    assert reopened.identity == original
    assert reopened.pairings["companion"] == record
    assert credentials(original, record).split(":")[1] == original["ed25519_seed"]
    assert (reopened.root / "tvpipe/credentials.txt").stat().st_mode & 0o777 == 0o600
    await pairing.stop()


async def test_health_separates_access_and_pairing(service):
    from atv_bridge_app.state import classify

    service.engine.adapter.access_restricted = True
    assert (await service.operations.tv_health())["error"]["code"] == "allow_access_changed"
    service.engine.adapter.access_restricted = False
    service.engine.problem = classify(Exception("HTTP 401"))
    assert (await service.operations.tv_health())["error"]["code"] == "pairing_removed"
    service.engine.problem = classify(TimeoutError())
    assert (await service.operations.tv_health())["error"]["code"] == "unreachable"


async def test_youtube_rejects_conflicting_content_identifier(service):
    client = await service.engine.ensure()
    calls = []

    async def wrong_video(target):
        client.app = (await client.apps())[0]
        client.media = {
            "state": "playing",
            "content_identifier": "abcdefghijk",
            "title": str(len(calls)),
        }
        calls.append(target)

    client.open = wrong_video
    result = await service.operations.tv_play_youtube(video_id="dQw4w9WgXcQ")
    assert not result["ok"] and len(calls) == 2


async def test_integration_contract(service):
    assert (await service.operations.tv_health())["pairing_needed"] == [
        "remote_pairing",
    ]
    assert (await service.engine.status())["volume_supported"] is True
    assert (await service.operations.tv_list_apps())["apps"][0]["allowed"]
    assert not (await service.tools.call("tv_open", {"app": "YouTube", "url": "youtube://watch/x"}))[
        "ok"
    ]
    assert (await service.tools.call("tv_play", {"youtube": "dQw4w9WgXcQ"}))["ok"]


async def test_stock_keepalive_uses_session_timer(monkeypatch):
    from unittest.mock import Mock

    from atv_bridge_app import atv

    monkeypatch.setattr(atv, "extras", None)
    facade = Mock()
    facade.settings.protocols.companion = object()
    facade.apps.get.return_value.api._connection.transport.get_extra_info.return_value = None
    facade.close.return_value = []
    session = atv.Session(facade, lambda *_: None)
    try:
        assert 14 < session.keepalive_timer.when() - asyncio.get_running_loop().time() <= 15
        session.keepalive_timer.cancel()
        session._send_noop()
        session.api._connection.send.assert_called_once_with(atv.FrameType.NoOp, b"")
        timer = session.keepalive_timer
    finally:
        await session.close()
    assert timer.cancelled()


async def test_one_airplay_pin_derives_all_sender_credentials(service):
    adapter = service.engine.adapter
    record = {"atv_id": "4142", "ltpk": "0f" * 32}
    adapter.begin_pair = AsyncMock(return_value=SimpleNamespace(close=AsyncMock()))
    adapter.finish_pair = AsyncMock(return_value=record)
    adapter.verify_reuse = AsyncMock()
    with pytest.raises(Exception, match="AirPlay must be paired"):
        await service.pairing.start("companion")
    prompt = await service.pairing.start("airplay")
    paired = await service.pairing.finish(prompt["pairing_id"], "2271")
    with pytest.raises(Exception, match="saved controller identity"):
        await service.pairing.start("companion")
    adapter.begin_pair.assert_awaited_once_with("airplay", None)
    assert paired["credential_reuse"] == {"companion": True, "raop": True}
    assert (
        paired["pairing_needed"]
        == (await service.operations.tv_health())["pairing_needed"]
        == ["remote_pairing"]
    )
    assert all(service.state.receiver(p) == record for p in ("airplay", "companion", "raop"))
    assert record["ltpk"] in (service.state.root / "tvpipe/credentials.txt").read_text()


async def test_read_screen_without_walk_and_press_focus_delivery(service):
    tv = await service.engine.ensure()
    tv.app = {"name": "TV", "bundle_id": "com.apple.TV"}

    async def focus(method):
        assert method == "ax_focused"
        return {"focused": {"caption": "TV", "label": "TV", "value": None}}

    service.observer.call = AsyncMock(side_effect=focus)
    screen = await service.tools.snapshot()
    assert screen["app"]["id"] == tv.app["bundle_id"] and screen["focus"]["label"] == "TV"
    await service.operations.tv_press(["left"], settle_ms=0)
    assert service.observer.call.await_count == 1
    result = await service.operations.tv_press(["up"], settle_ms=0, return_focus=True)
    assert result["delivered"] == "confirmed" and result["focused"]["caption"] == "TV"
    service.observer.call.side_effect = TimeoutError()
    result = await service.operations.tv_press(["down"], settle_ms=0, return_focus=True)
    assert (
        result["ok"]
        and result["delivered"] == "confirmed"
        and result["focused"] is None
        and result["focus_error"]
    )
    tv.lose_key = "menu"
    result = await service.operations.tv_press(["menu"], return_focus=True)
    assert result["delivered"] == "uncertain" and service.observer.call.await_count == 3
    assert tv.keys == ["left", "up", "down", "menu"]


async def test_airplay_only_record_and_inconclusive_companion_probe(service):
    record = {"atv_id": "4142", "ltpk": "0f" * 32}
    adapter = service.engine.adapter
    adapter.begin_pair = AsyncMock(return_value=SimpleNamespace(close=AsyncMock()))
    adapter.finish_pair = AsyncMock(return_value=record)
    adapter.verify_reuse = AsyncMock(side_effect=[TimeoutError(), None])
    prompt = await service.pairing.start("airplay")
    result = await service.pairing.finish(prompt["pairing_id"], "2271")
    assert result["credential_reuse"]["companion"] is None
    state = State(service.state.root)
    assert state.receiver("companion") == state.receiver("raop") == record
    assert state.pairing_needed() == ["remote_pairing"]


async def test_concurrent_press_waits_for_requested_focus(service):
    entered, release = asyncio.Event(), asyncio.Event()
    tv = await service.engine.ensure()

    async def focus(method):
        entered.set()
        await release.wait()
        return {"focused": None}

    service.observer.call = focus
    first = asyncio.create_task(service.operations.tv_press(["up"], settle_ms=0, return_focus=True))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(service.operations.tv_press(["down"], settle_ms=0))
        await asyncio.sleep(0)
        assert tv.keys == ["up"]
    finally:
        release.set()
        await first
    assert (await second)["ok"] and tv.keys == ["up", "down"]


async def test_screen_without_focus_does_not_guess_between_running_apps(service):
    service.observer.call = AsyncMock(
        side_effect=[
            {"items": [], "focused": None, "truncated": False},
            {
                "apps": [
                    {"bundle_id": "app.one", "name": "First"},
                    {"bundle_id": "app.two", "name": "Second"},
                ]
            },
        ]
    )
    service.state.save_pairings(remote_pairing=True)
    result = await service.tools.call("tv_look", {})
    assert not result["ok"] and not result["verified"]
    assert result["error"]["code"] == "screen_unavailable"
    assert result["screen"]["app"] is None and result["screen"]["focus"] is None
    assert result["data"]["items"] == [] and "image" not in result
    assert [call.args[0] for call in service.observer.call.await_args_list] == [
        "ax_items",
    ]
    assert service.observer.call.await_args_list[0].kwargs == {"max_items": 100}


async def test_status_reconnects_after_established_session_disconnects(service):
    client = await service.engine.ensure()
    client.status = AsyncMock(side_effect=ConnectionResetError())
    status = await service.engine.status()
    assert status["connected"] and client.closed
    assert len(service.engine.adapter.clients) == 2


async def test_repair_control_discards_only_local_control_credentials(service):
    state = service.state
    state.save_pairings(
        airplay={"atv_id": "old", "ltpk": "old"},
        companion="old",
        raop="old",
        remote_pairing={"path": "remote.plist"},
    )
    handler = AsyncMock()
    service.engine.adapter.begin_pair = AsyncMock(return_value=handler)
    try:
        assert (await service.pairing.start("airplay", replace=True))["state"] == "pin_required"
        for protocol in ("airplay", "companion", "raop"):
            assert not state.receiver(protocol)
        assert state.receiver("remote_pairing") == {"path": "remote.plist"}
        assert "receiver" not in (state.root / "tvpipe/credentials.txt").read_text()
    finally:
        await service.pairing.stop()


async def test_media_origin_denial_is_not_a_pairing_failure(service):
    from pyatv import exceptions

    url = "https://media.invalid/sample.mp4?private=example"
    protocol = SimpleNamespace(url_load_error=f"{url} returned HTTP 403")
    stream = SimpleNamespace(create_airplay_protocol=lambda: protocol)
    async def refused(media, position):
        stream.create_airplay_protocol()
        raise exceptions.PlaybackError(protocol.url_load_error)
    facade = SimpleNamespace(stream=SimpleNamespace(get=lambda _: stream, play_url=refused))
    tv = await service.engine.ensure()
    tv.play_url = lambda media, pos: Session.play_url(SimpleNamespace(facade=facade), media, pos)
    service.engine.adapter.supports_play_url = True
    service.tools.play_timeout = 2
    result = await service.tools.call("tv_play", {"media": url})
    assert result["error"]["code"] == "page_unreachable" and "HTTP 403" in result["error"]["message"]
    assert "private=example" not in str(result)


async def test_player_controls_use_advertised_protocol_and_skip_magnitude():
    from pyatv.const import FeatureName, Protocol

    session = object.__new__(Session)
    session.facade = Mock()
    available = {
        Protocol.MRP: {FeatureName.SetPosition, FeatureName.Stop, FeatureName.SetVolume},
        Protocol.Companion: {FeatureName.SkipBackward, FeatureName.SkipForward, FeatureName.SetVolume},
    }

    def features(protocol):
        return Mock(in_state=lambda state, feature: feature in available[protocol])

    session.facade.features.get.side_effect = features
    mrp, companion, audio = AsyncMock(), AsyncMock(), AsyncMock()
    session.facade.remote_control.get.side_effect = {Protocol.MRP: mrp, Protocol.Companion: companion}.get
    session.facade.audio.get.return_value = audio
    await session.seek(0, None)
    await session.seek(None, -30)
    await session.seek(None, 15)
    await session.hid("stop", 0)
    from pyatv.protocols.companion.api import MediaControlCommand
    session.api = AsyncMock()
    session.volume_sample = (0.0, None)
    session.api.mediacontrol_command.side_effect = [{}, {"_c": {"_vol": 0.25}}]
    assert await session.set_volume(25) == 25
    assert await session.read_companion_volume() == 25
    assert session.api.mediacontrol_command.await_count == 2
    mrp.set_position.assert_awaited_once_with(0)
    mrp.stop.assert_awaited_once()
    companion.skip_backward.assert_awaited_once_with(30)
    companion.skip_forward.assert_awaited_once_with(15)
    assert session.api.mediacontrol_command.await_args_list[0].args == (MediaControlCommand.SetVolume, {"_vol": 0.25})
    assert session.api.mediacontrol_command.await_args_list[1].args == (MediaControlCommand.GetVolume,)
    available[Protocol.MRP].clear()
    with pytest.raises(Fault, match="does not support seek_absolute"):
        await session.seek(10, None)
    mrp.set_position.assert_awaited_once()
