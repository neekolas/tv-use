import base64
import io
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from atv_bridge_observer import __main__ as observer_module


async def test_capture_shares_tunnel_and_scales(monkeypatch, tmp_path):
    monkeypatch.setenv("ATV_BRIDGE_HOST", "192.0.2.1")
    monkeypatch.setenv("ATV_CONTROLLER_ID", "test-controller")
    monkeypatch.setenv("ATV_BRIDGE_DATA", str(tmp_path))
    png = io.BytesIO()
    Image.new("RGB", (120, 80)).save(png, "PNG")
    dvt = AsyncMock()
    dvt.get_screenshot.return_value = png.getvalue()

    @asynccontextmanager
    async def channel(_):
        yield dvt

    provider_calls = []
    active = object()

    class Tunnel:
        def __init__(self, provider_factory):
            self.factory = provider_factory
            self.rsd = None

        async def aopen(self):
            if self.rsd is None:
                provider_calls.append(await self.factory())
                self.rsd = active
            return self.rsd

        async def aclose(self):
            self.rsd = None

    monkeypatch.setattr(observer_module, "UserspaceRsdTunnel", Tunnel)
    monkeypatch.setattr(observer_module, "DvtProvider", channel)
    monkeypatch.setattr(observer_module, "Screenshot", channel)
    monkeypatch.setattr(observer_module.Observer, "provider", AsyncMock(return_value="provider"))
    observer = observer_module.Observer()
    observer.record.touch()
    try:
        for width, expected in [(60, (60, 40)), (180, (120, 80))]:
            result = await observer.call({"method": "screenshot", "max_width": width})
            image = Image.open(io.BytesIO(base64.b64decode(result["png"])))
            assert image.size == expected and image.format == "PNG"
        assert provider_calls == ["provider"] and dvt.get_screenshot.await_count == 2
        assert await observer.call({"method": "health"}) == {"paired": True, "tunnel": True}
    finally:
        await observer.close()
    assert observer.tunnel.rsd is None


def packed(value):
    return {"ObjectType": "passthrough", "Value": value}


def element(identifier):
    return {
        "ObjectType": "AXAuditElement_v1",
        "Value": packed({"PlatformElementValue_v1": packed(identifier)}),
    }


async def test_passive_focus_and_hierarchy_share_session(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from atv_bridge_observer import accessibility
    from pymobiledevice3.services.accessibilityaudit import AccessibilityAudit

    for key, value in {
        "ATV_BRIDGE_HOST": "192.0.2.1",
        "ATV_CONTROLLER_ID": "test",
        "ATV_BRIDGE_DATA": str(tmp_path),
    }.items():
        monkeypatch.setenv(key, value)

    class Tunnel:
        def __init__(self, provider_factory):
            self.rsd = None

        async def aopen(self):
            self.rsd = self.rsd or SimpleNamespace(
                peer_info={"Services": {accessibility.SERVICE: {}}}
            )
            return self.rsd

        async def aclose(self):
            self.rsd = None

    sessions = []
    header = bytes.fromhex("2b00000001000000000000000000000000000000")
    row = bytes.fromhex("2b00000002000000000000000000000000000000")

    def node(ident, caption, role):
        return {
            "ObjectType": "AXAuditNode_v1",
            "Value": packed(
                {
                    "AuditElementValue_v1": element(ident),
                    "HumanReadableRoleDescriptionValue_v1": packed(role),
                    "HumanReadableDescriptionValue_v1": packed(caption),
                }
            ),
        }

    class Audit(AccessibilityAudit):
        def __init__(self, rsd):
            self.broken = self.closed = False
            self.root = header
            self.commands = []
            sessions.append(self)

        async def _ensure_ready(self):
            pass

        async def close(self):
            self.closed = True

        async def _invoke(self, command, *args, **kwargs):
            self.commands.append((command, args))
            if self.broken:
                raise ConnectionResetError()
            if command == "deviceFetchSpecialElement:":
                return element(self.root)
            if command == "deviceElement:valueForAttribute:":
                attr = args[1]["Value"]["Value"]["AttributeNameValue_v1"]["Value"]
                assert attr == "_AXHierarchyElementsAttribute"
                ident = args[0]["Value"]["Value"]["PlatformElementValue_v1"]["Value"]
                return node(ident, "Settings" if ident == header else "General", "Button")
            if command == "deviceRunningApplications":
                return packed(
                    [
                        {
                            "ObjectType": "AXAuditApplication_v1",
                            "Value": packed(
                                {
                                    "DisplayNameValue_v1": packed("Settings"),
                                    "PidValue_v1": packed(43),
                                    "BundleIdentifierValue_v1": packed("com.apple.TVSettings"),
                                }
                            ),
                        }
                    ]
                )
            assert command in {"deviceEnableHighlight:", "deviceInspectorShowVisuals:"}

    trees = []

    class Tree:
        def __init__(self, rsd):
            self.closed = False
            self.stall = False
            self.fail_once = False
            self.hit_pid = 43
            trees.append(self)

        async def close(self):
            self.closed = True

        async def displayed_pid(self, point):
            return self.hit_pid

        async def snapshot(self, pid):
            import asyncio

            if self.fail_once:
                self.fail_once = False
                raise ConnectionResetError()
            if self.stall:
                await asyncio.Event().wait()
            focused = {"uid": row.hex(), "caption": "General", "pid": pid}
            return {
                "app": {"pid": pid, "bundle_id": "com.apple.TVSettings"},
                "items": [{"uid": header.hex(), "caption": "Settings"}, focused],
                "focused": focused if pid == 43 else None,
            }

    monkeypatch.setattr(accessibility, "TreeChannel", Tree)
    monkeypatch.setattr(observer_module, "UserspaceRsdTunnel", Tunnel)
    monkeypatch.setattr(observer_module, "AccessibilityAudit", Audit)
    observer = observer_module.Observer()
    with pytest.raises(FileNotFoundError):
        await observer.call({"method": "ax_items"})
    observer.record.touch()
    try:
        assert (await observer.call({"method": "ax_focused"}))["focused"]["caption"] == "General"
        assert not sessions  # Native focus does not open or target the legacy inspector.
        result = await observer.call({"method": "ax_items"})
        assert [item["caption"] for item in result["items"]] == ["Settings", "General"]
        assert result["items"][1]["uid"] == result["focused"]["uid"]
        limited = await observer.call({"method": "ax_items", "max_items": 1})
        assert limited["truncated"] and limited["items"] == [limited["focused"]]
        assert (await observer.call({"method": "ax_running_apps"}))["apps"] == [
            {"bundle_id": "com.apple.TVSettings", "name": "Settings", "pid": 43}
        ]
        session = sessions[0]
        reader = accessibility.ScreenReader(session, SimpleNamespace(peer_info={"Services": {}}))
        await reader.start()
        partial = await reader.observe(60)
        assert partial["focused"] is None and partial["items"][0]["caption"] == "Settings"
        assert ("deviceInspectorShowVisuals:", (0,)) in session.commands
        assert ("deviceEnableHighlight:", (False,)) in session.commands
        trees[0].hit_pid = 44
        changed = await observer.call({"method": "ax_focused"})
        assert changed == {"focused": None, "app": {"pid": 44, "bundle_id": "com.apple.TVSettings"}}
        trees[0].fail_once = True
        assert (await observer.call({"method":"ax_focused"}))["focused"]["pid"] == 43
        assert trees[0].closed and session.closed and len(trees) == 2
        trees[-1].stall = True
        with pytest.raises(TimeoutError):
            await observer.read_ax({"method": "ax_focused", "timeout": 0.01})
        assert trees[-1].closed
    finally:
        await observer.close()
    assert all(session.closed for session in sessions)


async def test_observer_stderr_reaches_app_log(tmp_path, caplog):
    import logging
    import sys
    from types import SimpleNamespace
    from atv_bridge_app.processes import ObserverClient

    state = SimpleNamespace(root=tmp_path, identity={"controller_id": "test"})
    code = """import sys
for line in sys.stdin:
    print("AccessibilityAudit connection reset", file=sys.stderr, flush=True)
    print('{"ok":true}', flush=True)
"""
    client = ObserverClient("192.0.2.1", state, [sys.executable, "-c", code])
    with caplog.at_level(logging.WARNING, logger="atv_bridge.observer"):
        try:
            assert await client.call("ax_focused") == {"ok": True}
        finally:
            await client.close()
    assert "AccessibilityAudit connection reset" in caplog.text


def test_native_snapshot_distinguishes_remote_focus_from_selection():
    from atv_bridge_observer.native import screen_rows

    def row(uid, label, focused=False, visible=True):
        return {
            "UIAccessibilitySnapshotKeyElement": {"pid": 43, "objectID": uid},
            "UIAccessibilitySnapshotKeyAttributes": {
                2001: label,
                2094: focused,
                2025: visible,
                2018: not focused,
                2003: {"x": 10, "y": 20, "width": 30, "height": 40},
            },
        }

    root = row(1, "")
    first, second = row(2, "General"), row(3, "Remotes and Devices", True)
    root["UIAccessibilitySnapshotKeyChildren"] = [first, second, row(4, "Hidden", visible=False)]
    view = screen_rows(root, 43)
    assert [item["label"] for item in view["items"]] == ["General", "Remotes and Devices"]
    assert view["focused"]["label"] == "Remotes and Devices"
    first["UIAccessibilitySnapshotKeyAttributes"][2094] = True
    assert screen_rows(root, 43)["focused"] is None


@pytest.mark.parametrize("traits,selected", [(0, False), (2, False), (9, True), (None, None), (True, None), ("8", None)])
def test_native_selection_trait_independent_of_focus(traits, selected):
    from atv_bridge_observer.native import OPTIONS, screen_rows

    assert 2004 in OPTIONS["attributes"]
    attrs = {2001: "English (CC)", 2025: 1, 2094: 0}
    if traits is not None:
        attrs[2004] = traits
    view = screen_rows({"UIAccessibilitySnapshotKeyElement": {"pid": 5, "objectID": 1},
                        "UIAccessibilitySnapshotKeyAttributes": attrs}, 5)
    assert view["items"][0]["selected"] is selected and view["focused"] is None


@pytest.mark.parametrize("hit,expected", [
    ({"pid": 5, "objectID": 19}, True),
    ({"pid": 5, "objectID": 20}, False),
    ({"pid": 6, "objectID": 19}, False),
    (None, False),
])
async def test_menu_visibility_hit_tests_the_exported_row(hit, expected):
    from atv_bridge_observer.native import TreeChannel

    tree = TreeChannel(None)
    tree.read = AsyncMock(return_value=hit)
    row = {"pid": 5, "uid": "19", "frame": {"x": 100, "y": 40, "width": 40, "height": 10}}
    assert await tree.hits_row(row) is expected
    tree.read.assert_awaited_once_with(6, {"point": (120, 45)})


@pytest.mark.parametrize("picker_open", [True, False])
async def test_guarded_picker_read_rejects_background_rows(picker_open):
    from atv_bridge_observer.accessibility import ScreenReader
    from atv_bridge_observer.native import screen_rows

    def node(uid, label=None, *children, focused=False):
        return {
            "UIAccessibilitySnapshotKeyElement": {"pid": 5, "objectID": uid},
            "UIAccessibilitySnapshotKeyAttributes": {2001: label, 2025: 1, 2094: focused},
            "UIAccessibilitySnapshotKeyChildren": children,
        }

    picker = node(2, None, node(7, None, node(3, "Off", focused=picker_open)), node(4, "English (CC)"))
    picker["UIAccessibilitySnapshotKeyAttributes"][5003] = "UITableView"
    root = node(1, "Netflix", picker, node(5, "Open picker", focused=not picker_open))
    reader = ScreenReader(None, None)
    reader.tree = AsyncMock()
    reader.tree.hits_row.return_value = True
    reader.displayed_screen = AsyncMock(return_value=screen_rows(root, 5))
    screen = await reader.observe(100, "English (CC)")
    english = next(row for row in screen["items"] if row["label"] == "English (CC)")
    assert english["on_screen"] is picker_open


def test_native_channel_encodes_only_observation_requests():
    from atv_bridge_observer.ax_archive import decode, request

    token = {"pid": 43, "objectID": 123, "isApplicationElement": True}
    lookup = decode(request(1, {"pid": 43}))
    assert lookup["parameters"] == {"pid": 43}
    read = decode(request(2, {"parameter": {"attributes": [2094]}}, translation=token))
    assert read["requestType"] == 2 and read["attributeType"] == 52 and read["actionType"] == 0
    assert read["translation"]["objectID"] == 123
    with pytest.raises(ValueError):
        request(7, {}, translation=token)
    hit = decode(request(6, {"point": (960, 540)}))
    assert hit["requestType"] == 6 and hit["actionType"] == 0
    assert hit["parameters"]["displayId"] == 0


async def test_native_channel_reuses_dtx_and_matches_semantic_reply(monkeypatch):
    """Exercise control-channel dispatch and the inner keyed archives."""
    from types import SimpleNamespace
    from pymobiledevice3.dtx.service import DTXControlService
    from atv_bridge_observer import native
    from atv_bridge_observer.ax_archive import decode

    process = {"pid": 45, "objectID": 123, "isApplicationElement": True}
    record = {
        "UIAccessibilitySnapshotKeyElement": process,
        "UIAccessibilitySnapshotKeyAttributes": {2001: "YouTube", 2094: True},
    }
    connections = []
    install = DTXControlService.__init__

    def init(service, ctx):
        if ctx != "test":
            return install(service, ctx)

    monkeypatch.setattr(DTXControlService, "__init__", init)

    class Connection:
        def __init__(self, reader, writer):
            self._control_svc = SimpleNamespace(_ctx="test")
            self._services = {}
            self._ctrl_channel = self
            self.closed = self.mismatch = False
            self.subscriptions = []
            connections.append(self)

        async def connect(self):
            pass

        async def close(self):
            self.closed = True

        async def invoke(self, name, value=None):
            if name == "deviceAPIVersion":
                assert await self._services[0].version() == 7
                return 26
            if name == "clientNeedsAccessibility:":
                self.subscriptions.append(value)
                return
            assert name == "processDataFromHost:"
            incoming = decode(value)
            kind = incoming["requestType"]
            # Native bulk updates can interleave with either request.
            await self._services[0].packet({"associatedRequestType": 11})
            await self._services[0].packet(
                {
                    "associatedRequestType": kind,
                    "error": 0,
                    "attribute": 52,
                    "associatedTranslationObject": process
                    | {"objectID": 124 if self.mismatch else 123},
                    "resultData": process if kind == 1 else record,
                }
            )

    monkeypatch.setattr(native, "DTXConnection", Connection)
    monkeypatch.setattr(native, "decode", lambda reply: reply)
    rsd = SimpleNamespace(
        peer_info={"Services": {native.SERVICE: {"Port": "5000"}}},
        create_service_connection=AsyncMock(return_value=SimpleNamespace(reader=None, writer=None)),
    )
    channel = native.TreeChannel(rsd)
    try:
        for _ in range(2):
            assert (await channel.snapshot(45))["focused"]["label"] == "YouTube"
        assert len(connections) == 1
        connections[0].mismatch = True
        with pytest.raises(ValueError, match="belongs to another snapshot"):
            await channel.snapshot(45)
    finally:
        await channel.close()
    assert connections[0].closed and connections[0].subscriptions == [True, False]


async def test_display_hit_follows_app_across_home_and_settings():
    from types import SimpleNamespace
    from atv_bridge_observer.accessibility import ScreenReader, DISPLAY_POINTS
    from atv_bridge_observer.native import NoDisplayElement

    reader = ScreenReader(None, SimpleNamespace(peer_info={"Services": {}}))
    tree = reader.tree = AsyncMock()

    def screen(pid):
        bundle, focus = {
            41: ("com.apple.PineBoard", None),
            51: ("com.apple.HeadBoard", "YouTube"),
            61: ("com.apple.TVSettings", "Remotes and Devices"),
        }[pid]
        row = {"label": focus or bundle, "uid": str(pid)}
        return {"app": {"bundle_id": bundle}, "items": [row], "focused": row if focus else None}

    tree.snapshot.side_effect = screen
    tree.displayed_pid.side_effect = [51, NoDisplayElement(), 41, 41, 61, 51]
    for bundle, label in [
        ("com.apple.HeadBoard", "YouTube"),
        ("com.apple.TVSettings", "Remotes and Devices"),
        ("com.apple.HeadBoard", "YouTube"),
    ]:
        observed = await reader.observe(10)
        assert observed["app"]["bundle_id"] == bundle and observed["focused"]["label"] == label
    assert [c.args[0] for c in tree.snapshot.await_args_list] == [51, 41, 61, 51]
    assert tree.displayed_pid.await_args_list[0].args == (DISPLAY_POINTS[0],)


async def test_app_switch_excludes_status_reads_and_preserves_tunnel():
    import asyncio
    from types import SimpleNamespace
    from atv_bridge_app.processes import ObserverClient

    observer = object.__new__(observer_module.Observer)
    observer.reader = AsyncMock()
    observer.tunnel = AsyncMock()
    reader = observer.reader
    await observer.call({"method": "ax_release"})
    reader.close.assert_awaited_once()
    assert observer.reader is None
    observer.tunnel.aclose.assert_not_called()

    client = object.__new__(ObserverClient)
    client.lock = asyncio.Lock()
    client.proc = SimpleNamespace(returncode=None)
    client._call_locked = AsyncMock(return_value={})
    async with client.app_switch():
        client._call_locked.assert_awaited_once_with("ax_release", {})
        read = asyncio.create_task(client.call("ax_focused"))
        await asyncio.sleep(0)
        assert not read.done()
    await read
    assert [c.args[0] for c in client._call_locked.await_args_list] == ["ax_release", "ax_focused"]


async def test_remote_bonjour_uses_authenticated_advertisement(monkeypatch, tmp_path):
    import plistlib
    from types import SimpleNamespace
    from pymobiledevice3.remote.siphash import compute_auth_tag

    for key, value in {
        "ATV_BRIDGE_HOST": "",
        "ATV_BRIDGE_BONJOUR": "1",
        "ATV_CONTROLLER_ID": "test",
        "ATV_BRIDGE_DATA": str(tmp_path),
    }.items():
        monkeypatch.setenv(key, value)
    observer = observer_module.Observer()
    irk = b"a" * 16
    observer.record.write_bytes(plistlib.dumps({observer_module.tunnel_service.PEER_ALT_IRK_KEY: irk}))

    def answer(host, key):
        tag = base64.b64encode(compute_auth_tag(key, "rotating-id")).decode()
        return SimpleNamespace(
            properties={"identifier": "rotating-id", "authTag": tag},
            addresses=[SimpleNamespace(full_ip=host)],
            port=50123,
        )

    browse = AsyncMock(return_value=[answer("192.0.2.10", b"b" * 16), answer("192.0.2.22", irk)])
    monkeypatch.setattr(observer_module, "browse_remotepairing", browse)
    assert await observer.tunnel_endpoint() == ("192.0.2.22", 50123)
    await observer.close()


async def test_screen_health_probes_saved_record_and_replacement_removes_it(tmp_path, monkeypatch):
    from types import SimpleNamespace

    for key, value in {
        "ATV_BRIDGE_HOST": "192.0.2.1",
        "ATV_CONTROLLER_ID": "test",
        "ATV_BRIDGE_DATA": str(tmp_path),
    }.items():
        monkeypatch.setenv(key, value)
    observer = observer_module.Observer()
    observer.record.touch()
    observer.tunnel = AsyncMock()
    rsd = SimpleNamespace(peer_info={"Services": {}})
    observer.tunnel.aopen.return_value = rsd
    with pytest.raises(observer_module.ScreenPairingInvalid, match="services"):
        await observer.call({"method": "health", "verify": True})
    rsd.peer_info["Services"] = {
        observer_module.DvtProvider.RSD_SERVICE_NAME: {},
        observer_module.accessibility.SERVICE: {},
    }
    assert (await observer.call({"method": "health", "verify": True}))["paired"]

    async def start():
        assert not observer.record.exists()
        return {}

    observer.pair_start = start
    await observer.call({"method": "pair_start", "replace": True})


async def test_health_and_status_classify_invalid_screen_pairing(service):
    from atv_bridge_app.state import Fault

    service.state.save_pairings(remote_pairing={"path": "remote.plist"})
    await service.engine.ensure()
    service.engine.last_power = "on"
    service.observer.call = AsyncMock(
        side_effect=Fault("screen_pairing_invalid", "Rejected", "Re-pair")
    )
    health = await service.operations.tv_health()
    result = (await service.tools.tv_status())["data"]
    for report in (result, health):
        assert any(
            i["code"] == "screen_pairing_invalid" and "Re-pair screen" in i["fix"]
            for i in report["problems"]
        )
    assert result["pairings"]["screen"] == "invalid" and not result["capabilities"]["look_image"]
    assert "remote_pairing" in health["pairing_needed"]
    service.observer.call.side_effect = Fault(
        "observer_unavailable", "Network unavailable", "Retry"
    )
    assert not any(
        i["code"] == "screen_pairing_invalid"
        for i in (await service.operations.tv_health())["problems"]
    )


async def test_tunnel_pin_verification_rejection_has_its_own_classification(tmp_path, monkeypatch):
    from pymobiledevice3.remote import tunnel_service

    for key, value in {
        "ATV_BRIDGE_HOST": "192.0.2.1",
        "ATV_CONTROLLER_ID": "test",
        "ATV_BRIDGE_DATA": str(tmp_path),
    }.items():
        monkeypatch.setenv(key, value)
    observer = observer_module.Observer()
    observer.tunnel_endpoint = AsyncMock(return_value=("192.0.2.1", 1234))
    base = tunnel_service.RemotePairingTunnelService
    monkeypatch.setattr(base, "_validate_pairing", AsyncMock(return_value=False))

    async def connect(self, autopair):
        assert not autopair
        await self._validate_pairing()

    monkeypatch.setattr(base, "connect", connect)
    with pytest.raises(observer_module.ScreenPairingInvalid, match="rejected"):
        await observer.provider()
    monkeypatch.setattr(base, "connect", AsyncMock(side_effect=ConnectionRefusedError()))
    with pytest.raises(ConnectionRefusedError):
        await observer.provider()


async def test_large_frame_budget_applies_only_to_the_capture_connection(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from pymobiledevice3.dtx.exceptions import DTXProtocolError

    reader = observer_module.dtx_reader
    fragment = SimpleNamespace(data_size=39167470, identifier=3, count=2)
    start_read = asyncio.Event()
    async def assemble_frame():
        await start_read.wait()
        return reader.DTXFragmenter(fragment, 0, reader.MAX_BUFFERED_SIZE).declared_size

    task = asyncio.create_task(assemble_frame())
    provider = SimpleNamespace(dtx=SimpleNamespace(_reader_task=task))

    @asynccontextmanager
    async def channel(_):
        async def capture():
            with pytest.raises(DTXProtocolError, match="31457280"):
                reader.DTXFragmenter(fragment, 0, reader.MAX_BUFFERED_SIZE)
            start_read.set()
            return await task
        yield SimpleNamespace(get_screenshot=capture)

    monkeypatch.setattr(observer_module, "Screenshot", channel)
    original = reader.DTXFragmenter
    try:
        assert await observer_module.capture_frame(provider) == fragment.data_size
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert reader.DTXFragmenter is original
