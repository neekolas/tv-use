import asyncio
import json
from datetime import timedelta

import pytest
from homeassistant.components.media_player import MediaPlayerEntityFeature
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util.dt import utcnow
from pytest_homeassistant.common import async_fire_time_changed

from custom_components.atv_bridge.const import DOMAIN
from custom_components.atv_bridge.diagnostics import async_get_config_entry_diagnostics


async def test_entities_and_commands(hass, entry, bridge):
    def entity(domain, key):
        return er.async_get(hass).async_get_entity_id(
            domain, DOMAIN, f"{entry.entry_id}_{key}"
        )

    player = entity("media_player", "player")
    remote = entity("remote", "remote")
    state = hass.states.get(player)
    assert state.state == "playing"
    assert state.attributes["source"] == "TV"
    assert state.attributes["source_list"] == ["TV", "Music"]
    assert state.attributes["media_title"] == "News"
    assert state.attributes["supported_features"] & MediaPlayerEntityFeature.VOLUME_STEP
    assert hass.states.get(entity("sensor", "health")).attributes["ok"]
    assert hass.states.get(entity("binary_sensor", "connected")).state == "on"
    assert hass.states.get(entity("binary_sensor", "keyboard_focused")).state == "off"
    for action, key in (
        ("media_play", "play"),
        ("media_pause", "pause"),
        ("media_next_track", "next"),
        ("media_previous_track", "previous"),
        ("volume_up", "volume_up"),
        ("volume_down", "volume_down"),
    ):
        await hass.services.async_call(
            "media_player", action, {"entity_id": player}, blocking=True
        )
        assert ("/api/tool/tv_remote", {"keys": [key]}) in bridge.calls
    for media_type, media_id, field in (
        ("url", "https://example.com/movie.mp4", "media"),
        ("url", "https://tv.apple.com/movie/xyz", "link"),
        ("url", "https://youtu.be/jNQXAC9IVRw", "youtube"),
        ("app", "TV", "app"),
    ):
        await hass.services.async_call(
            "media_player",
            "play_media",
            {
                "entity_id": player,
                "media_content_type": media_type,
                "media_content_id": media_id,
            },
            blocking=True,
        )
        tool = "tv_open" if media_type == "app" else "tv_play"
        assert (f"/api/tool/{tool}", {field: media_id}) in bridge.calls
    for action, on in (("turn_on", True), ("turn_off", False)):
        await hass.services.async_call(
            "remote", action, {"entity_id": remote}, blocking=True
        )
        assert ("/api/tool/tv_power", {"on": on}) in bridge.calls
    await hass.services.async_call(
        "button", "press", {"entity_id": entity("button", "self_test")}, blocking=True
    )
    assert ("/api/tool/tv_status", {}) in bridge.calls
    bridge.replies["/api/tool/tv_remote"] = {
        "ok": False,
        "verified": False,
        "error": {"code": "uncertain", "message": "Delivery uncertain."},
        "evidence": [],
    }
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "remote",
            "send_command",
            {"entity_id": remote, "command": ["menu"], "num_repeats": 2},
            blocking=True,
        )
    assert (
        sum(
            path.endswith("/tv_remote") and args.get("keys") == ["menu"]
            for path, args in bridge.calls
        )
        == 1
    )
    bridge.replies["/api/tool/tv_power"] = {
        "ok": False,
        "state": "failed",
        "error": {"message": "Wake failed"},
    }
    with pytest.raises(HomeAssistantError, match="Wake failed"):
        await hass.services.async_call(
            "remote", "turn_on", {"entity_id": remote}, blocking=True
        )
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert bridge.config["token"] not in json.dumps(diag)
    assert diag["status"]["connected"]


async def test_websocket_reconnect_and_poll(hass, bridge, entry):
    initial = await asyncio.wait_for(bridge.sockets.get(), 3)
    await initial.close()
    socket = await asyncio.wait_for(bridge.sockets.get(), 5)
    bridge.status.update(power="off", keyboard_focused=True)
    await socket.send_json({"event": "status_changed"})
    async with asyncio.timeout(15):
        while (
            hass.states.get("media_player.apple_tv_agent_bridge_player").state != "off"
        ):
            await asyncio.sleep(0.05)
    bridge.status.update(power="on", volume_supported=False)
    async_fire_time_changed(hass, utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done()
    state = hass.states.get("media_player.apple_tv_agent_bridge_player")
    assert state.state == "playing"
    assert (
        not state.attributes["supported_features"]
        & MediaPlayerEntityFeature.VOLUME_STEP
    )


async def test_seek_stop_and_absolute_volume(hass, entry, bridge):
    player = er.async_get(hass).async_get_entity_id("media_player", DOMAIN, f"{entry.entry_id}_player")
    bridge.status.update(
        playback_controls={"seek_absolute": True, "stop": True, "set_volume": True},
        volume_percent=38, position_updated_at="2026-11-25T10:03:00+00:00",
    )
    bridge.status["now_playing"].update(position_s=120, duration_s=300)
    await entry.runtime_data.async_refresh()
    attrs = hass.states.get(player).attributes
    features = MediaPlayerEntityFeature
    assert attrs["supported_features"] & (features.SEEK | features.STOP | features.VOLUME_SET) == (
        features.SEEK | features.STOP | features.VOLUME_SET
    )
    assert (attrs["media_position"], attrs["media_duration"], attrs["volume_level"]) == (120, 300, 0.38)
    assert attrs["media_position_updated_at"].isoformat() == bridge.status["position_updated_at"]
    for action, data, tool, args in [
        ("media_seek", {"seek_position": 12.6}, "tv_seek", {"position_s": 13}),
        ("media_stop", {}, "tv_remote", {"keys": ["stop"]}),
        ("volume_set", {"volume_level": 0.25}, "tv_volume", {"percent": 25}),
    ]:
        await hass.services.async_call("media_player", action, {"entity_id": player, **data}, blocking=True)
        assert (f"/api/tool/{tool}", args) in bridge.calls
