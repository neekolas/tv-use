import asyncio
import json
import time
from unittest.mock import AsyncMock

from jsonschema import Draft202012Validator
import pytest

from atv_bridge_app.fleet import Fleet
from atv_bridge_app.present import browsers
from atv_bridge_app.server import TVRuntime
from atv_bridge_app.state import State
from conftest import FakeAdapter


def check_property_descriptions(node, path):
    if isinstance(node, list):
        for child in node:
            check_property_descriptions(child, path)
    if isinstance(node, dict):
        for name, prop in node.get("properties", {}).items():
            assert prop.get("description", "").strip(), f"{path}.{name}"
        for key, child in node.items():
            check_property_descriptions(child, f"{path}.{key}")


def rooms(tmp_path, limit=1, **options):
    def factory(host, state, **options):
        adapter = FakeAdapter(state)
        adapter.host = adapter.resolver.address = host
        observer = AsyncMock()
        observer.call = AsyncMock(return_value={"focused": None, "app": None, "apps": []})
        return TVRuntime(host, state, adapter, observer, **options)

    return Fleet(
        tmp_path,
        {
            **options,
            "max_active_mirrors": limit,
            "apple_tvs": [
                {"name": "Living Room", "host": "192.0.2.15", "id": "AA:01"},
                {"name": "Bedroom", "host": "192.0.2.159", "id": "BB:02"},
            ],
        },
        factory=factory,
        stage_port=0,
    )


async def test_caller_scripts_can_be_disabled_across_receivers(tmp_path):
    fleet = rooms(tmp_path, page_scripts=False)
    for runtime in fleet.runtimes.values():
        reply = await runtime.tools.call("tv_page_run", {"script": "document.title"})
        assert reply["error"]["code"] == "scripts_disabled"


def test_mirror_options_reach_every_sender(tmp_path):
    # The code defaults differ from config.yaml, so a key that the fleet drops changes the mirror.
    fleet = rooms(tmp_path, mirror_size="1280x720", mirror_fps=12, mirror_fps_muted=20)
    for runtime in fleet.runtimes.values():
        mirror = runtime.operations.live_mirror
        assert (mirror.size, mirror.fps, mirror.fps_muted) == ("1280x720", 12, 20)


def test_migration_preserves_records_and_never_overwrites_new_pairings(tmp_path):
    old = State(tmp_path)
    old.save_pairings(
        airplay={"atv_id": "receiver", "ltpk": "key"},
        tv={
            "apple_tv_id": "AA:01",
            "name": "Living Room",
            "last_known_host": "192.0.2.15",
            "identifiers": ["AA:01"],
        },
        remote_pairing={"path": str(tmp_path / "observer/remote.plist")},
    )
    (tmp_path / "observer").mkdir()
    (tmp_path / "observer/remote.plist").write_bytes(b"saved record")
    fleet = rooms(tmp_path)
    a, b = fleet.select("aa:01"), fleet.select("BEDROOM")
    assert a.state.root == tmp_path / "tvs" / a.metadata["id"]
    assert a.state.token == b.state.token == old.token
    assert a.state.identity == b.state.identity == old.identity
    assert not (b.state.root / "identity.json").exists()
    assert a.state.receiver("airplay") == old.receiver("airplay") and not b.state.receiver(
        "airplay"
    )
    assert a.state.receiver("remote_pairing")["path"] == str(a.state.root / "observer/remote.plist")
    assert (a.state.root / "observer/remote.plist").read_bytes() == b"saved record"
    a.state.save_pairings(airplay={"atv_id": "new"})
    second = rooms(tmp_path)
    assert len(second.inventory()) == 2 and second.select().state.receiver("airplay") == {
        "atv_id": "new"
    }
    assert json.loads((tmp_path / "single-tv-backup/pairings.json").read_text()) == old.pairings


async def test_routing_and_independent_locks(tmp_path):
    fleet = rooms(tmp_path)
    a, b = fleet.runtimes.values()
    for r in (a, b):
        r.tools.tv_look = AsyncMock(return_value={"ok": True, "data": {}, "screen": {}})
        r.tools.tv_status = AsyncMock(return_value={"ok": True, "data": {"tv": {}}})
    a.state.save_pairings(airplay={"ltpk": "a"})
    assert (await fleet.call("tv_status", {}))["data"]["tv"]["name"] == "Living Room"
    b.state.save_pairings(airplay={"ltpk": "b"})
    for name, args in [("tv_sequence", {"steps": [{"key": "up"}]}), ("tv_remote", {"keys": ["up"]}), ("tv_show", {}), ("tv_hide", {}),
                       ("tv_page_run", {"script": "document.title"})]:
        result = await fleet.call(name, args)
        assert result["error"]["code"] == "tv_required" and len(result["data"]["tvs"]) == 2
    for name in ("tv_status", "tv_look"):
        assert len((await fleet.call(name, {}))["data"]["tvs"]) == 2
    async with a.tools.lock:
        a.tools.wait_timeout = 0.02
        assert (await fleet.call("tv_status", {"tv": "living room"}))["error"]["code"] == "busy"
        assert (await fleet.call("tv_status", {"tv": "bb:02"}))["ok"]
    assert (await fleet.call("tv_look", {"tv": "missing"}))["error"]["code"] == "tv_not_found"


async def test_routed_results_report_a_page_that_the_tv_ended(tmp_path):
    fleet = rooms(tmp_path)
    living = fleet.select("Living Room")
    mirror = living.operations.live_mirror
    await living.stage.show("a" * 32, "/c/a/index.html")
    living.operations.presenter = mirror
    end = {"reason": "ended_on_tv", "at": "2026-11-21T19:00:00+00:00", "page_id": "a" * 32}
    await living.tools.dismiss_ended(
        mirror, mirror.runner, end, time.monotonic(), ["tvpipe reported that the TV ended the session"]
    )
    assert living.stage.page is None and living.operations.presentation_end == end
    run = await fleet.call("tv_page_run", {"tv": "Living Room", "script": "document.title"})
    assert run["error"]["code"] == "nothing_showing"
    look = await fleet.call("tv_look", {"tv": "Living Room"})
    for result in (run, look):
        assert result["screen"]["tv"] == "Living Room" and result["screen"]["presenting"] is None
        assert result["screen"]["last_presentation_end"] == end


async def test_fleet_maps_the_keys_alias_before_routing(tmp_path):
    fleet = rooms(tmp_path)
    living = fleet.select("Living Room")
    living.tools.call = AsyncMock(return_value={"ok": True})
    assert (await fleet.call("tv_show", {"tv": "Living Room", "html": "x", "remote": "keys"}))["ok"]
    living.tools.call.assert_awaited_once_with("tv_show", {"html": "x", "remote": "focus"}, replace=False)


@pytest.mark.parametrize("limit", [1, 2])
async def test_mirror_admission_replacement_and_hide(tmp_path, limit):
    fleet = rooms(tmp_path, limit)
    a, b = fleet.runtimes.values()
    for rt in (a, b):
        rt.state.save_pairings(airplay={"ltpk": "paired"})

        async def show(*, rt=rt, **args):
            await rt.stage.show("test", "/test")
            return {"mode": "mirror", "page_id": "test", "verified": True, "evidence": ["frame"]}

        rt.operations.present = show
        rt.tools.snapshot = AsyncMock(return_value={})
    # This test is about mirror slots. Its fake present starts no remote input.
    assert (await fleet.call("tv_show", {"tv": "Living Room", "html": "1", "remote": "off"}))["ok"]
    b.tools.action_evidence = ["Previous call"]
    second = await fleet.call("tv_show", {"tv": "Bedroom", "html": "2", "remote": "off"})
    if limit == 1:
        assert second["error"]["code"] == "mirror_busy"
        assert "Previous call" not in second["evidence"]
        assert second["data"]["holders"][0]["name"] == "Living Room" and a.stage.page
        # An eviction takes over the presentation, so an end record of the Living Room does not survive it.
        a.operations.presentation_end = {"reason": "ended_on_tv", "at": "2026-11-21T19:00:00+00:00", "page_id": "old"}
        second = await fleet.call("tv_show", {"tv": "Bedroom", "html": "2", "remote": "off", "replace": True})
        assert a.stage.page is None and a.operations.presentation_end is None
    else:
        assert a.stage.page
    assert second["ok"] and second["screen"]["tv"] == "Bedroom"
    assert (await fleet.call("tv_show", {"tv": "Bedroom"}))["ok"]
    assert b.metadata["id"] not in fleet.mirrors
    assert bool(a.stage.page) == (limit == 2)
    for r in (a, b):
        await r.engine.stop()


def test_sweep_protects_all_live_browser_families():
    def row(parent, name):
        return browsers.Process(parent, 1, name, 0, False)

    table = {
        10: row(1, "tvpipe"),
        11: row(10, "chromium"),
        20: row(1, "tvpipe"),
        21: row(20, "chromium"),
        30: row(1, "chrome_crashpad"),
    }
    assert set(browsers.stray_processes(table, 10, {20})) == {30}


async def test_fleet_resources_and_scoped_api(tmp_path, aiohttp_client):
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    fleet = rooms(tmp_path, page_network_allow=["192.0.2.8:8443"])
    a, b = fleet.runtimes.values()
    for rt in (a, b):
        rt.state.save_pairings(airplay={"ltpk": "test"})
        rt.tools.tv_look = AsyncMock(
            return_value={"ok": True, "screen": {"focus": {"label": "Tile"}}, "data": {}}
        )
    client = await aiohttp_client(fleet.app())
    headers = {"Authorization": "Bearer " + fleet.controller.token}
    inventory = await (await client.get("/api/tvs", headers=headers)).json()
    assert [t["name"] for t in inventory["tvs"]] == ["Living Room", "Bedroom"]
    from urllib.parse import urlsplit
    ports = {urlsplit(rt.stage.origin).port for rt in (a, b)}
    assert a.stage.stage_ports == b.stage.stage_ports == ports
    assert a.stage.page_network is b.stage.page_network is fleet.page_network
    for port in ports:
        assert await fleet.page_network.resolve("127.0.0.1", port) == ["127.0.0.1"]
    assert await fleet.page_network.resolve("192.0.2.8", 8443) == ["192.0.2.8"]
    assert a.stage.origin != b.stage.origin
    assert a.stage.media_origin == b.stage.media_origin
    for rt in (a, b):
        (rt.stage.media / "clip.mp4").write_bytes(rt.metadata["id"].encode())
    async with httpx2.AsyncClient() as media:
        for rt in (a, b):
            event = rt.stage.media_reads["clip.mp4", "127.0.0.1", "test"] = asyncio.Event()
            response = await media.get(
                a.stage.media_origin + "/m/" + rt.metadata["id"] + "/clip.mp4?session=test",
                headers={"Range": "bytes=0-5"},
            )
            assert response.status_code == 206 and response.text == rt.metadata["id"][:6]
            assert event.is_set()
    async with (
        httpx2.AsyncClient(headers=headers) as http,
        streamable_http_client(str(client.make_url("/mcp")), http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        listed = await session.list_tools()
        assert {tool.name for tool in listed.tools} == {
            "tv_show", "tv_play", "tv_open", "tv_look", "tv_navigate",
            "tv_remote", "tv_sequence", "tv_power", "tv_pair", "tv_status", "tv_page_run",
            "tv_history", "tv_seek", "tv_volume",
        }
        for tool in listed.tools:
            assert tool.description and tool.description.strip(), tool.name
            assert "tv" in tool.input_schema["properties"]
            check_property_descriptions(tool.input_schema, tool.name)
            example = tool.description.split(f"For example, {tool.name} ")[1].removesuffix(".")
            Draft202012Validator(tool.input_schema).validate(json.loads(example))
        resources = (await session.list_resources()).resources
        assert all(r.description and r.description.strip() for r in resources)
        prompt, = (await session.list_prompts()).prompts
        assert prompt.description.strip() and prompt.arguments[0].description.strip()
        uris = {str(r.uri).rstrip("/") for r in resources}
        assert "tv://tvs" in uris and "tv://apps/" + b.metadata["id"] in uris
        screen = await session.read_resource("tv://screen/" + a.metadata["id"])
        assert json.loads(screen.contents[0].text)["screen"]["tv"] == "Living Room"
        apps = await session.read_resource("tv://apps/" + b.metadata["id"])
        assert json.loads(apps.contents[0].text)[0]["name"] == "YouTube"
        result = await session.call_tool("tv_show", {"tv": "Bedroom"})
        assert result.structured_content, result.content
        assert result.structured_content["screen"]["tv_id"] == b.metadata["id"]
    bad = await (
        await client.post(
            "/api/tvs/" + b.metadata["id"] + "/pair/start",
            headers=headers,
            json={"protocol": "airplay", "apple_tv_id": "AA:01", "replace": True},
        )
    ).json()
    assert not bad["ok"] and b.state.receiver("airplay") == {"ltpk": "test"}


async def test_delete_receiver_drains_requests_and_suppresses_configured_record(tmp_path, aiohttp_client):
    fleet = rooms(tmp_path)
    first, other = fleet.runtimes.values()
    other.state.save_pairings(airplay={"ltpk": "other"})
    client = await aiohttp_client(fleet.app())
    headers = {"Authorization": "Bearer " + fleet.controller.token}
    path = "/api/tvs/" + first.metadata["id"]
    assert (await client.delete(path)).status == 401
    assert first.state.root.exists()
    with first.use():
        deleting = asyncio.create_task(client.delete(path, headers=headers))
        async with asyncio.timeout(2):
            while not first.retired:
                await asyncio.sleep(0)
        assert not deleting.done()
    assert (await (await deleting).json())["ok"]
    assert (await (await client.delete(path, headers=headers)).json())["ok"]
    assert not first.state.root.exists() and first not in fleet.runners
    assert (await (await client.get(path + "/status", headers=headers)).json())["error"]["code"] == "tv_not_found"
    assert (await (await client.get("/api/status", headers=headers)).json())["ok"]
    reopened = rooms(tmp_path)
    assert [row["name"] for row in reopened.inventory()] == ["Bedroom"]
    assert reopened.select().state.receiver("airplay") == {"ltpk": "other"}
    reopened.candidates = AsyncMock(return_value=[{
        "name": first.metadata["name"], "identifiers": [first.metadata["identifier"]],
        "address": first.metadata["host"],
    }])
    registered = await reopened.enrol(first.metadata["identifier"])
    assert registered["id"] == first.metadata["id"] and not registered["paired"]
    assert {room["name"] for room in rooms(tmp_path).inventory()} == {"Living Room", "Bedroom"}


def test_option_removal_preserves_ha_enrolments_and_allows_readding(tmp_path):
    fleet = rooms(tmp_path)
    original, other = fleet.runtimes.values()
    original.state.save_pairings(airplay={"ltpk": "old"})
    added = fleet.register({"name": "Office", "host": "192.0.2.30"})
    added.state.save_pairings(remote_pairing={"test": True})
    options = fleet.options | {"apple_tvs": fleet.entries[1:]}
    reopened = Fleet(tmp_path, options, factory=fleet.factory)
    assert {row["name"] for row in reopened.inventory()} == {"Office", "Bedroom"}
    assert not original.state.root.exists() and other.state.root.exists()
    assert reopened.select("Office").state.receiver("remote_pairing") == {"test": True}
    restored = rooms(tmp_path)
    assert not restored.select("Living Room").state.receiver("airplay")
    assert restored.controller.identity == fleet.controller.identity
