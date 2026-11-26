# SPDX-License-Identifier: GPL-3.0-or-later
"""Read the visible app's native AX tree over the existing paired RSD tunnel."""

import asyncio
import logging
from contextlib import suppress

from pymobiledevice3.dtx import DTXConnection
from pymobiledevice3.dtx.service import DTXControlService, dtx_on_invoke

from .ax_archive import decode, request

SERVICE = "com.apple.accessibility.axAuditDaemon.remoteAXService"
log = logging.getLogger(__name__)
# AXRuntime kAXXCAttributeLabel, Frame, Traits, Identifier, Value, IsVisible,
# HasNativeFocus, ElementClassName, and ApplicationBundleIdentifier.
OPTIONS = {
    "attributes": [2001, 2003, 2004, 5019, 2006, 2025, 2094, 5003, 3003],
    "maxDepth": 40,
    "maxChildren": 256,
    "maxArrayCount": 256,
    "traverseFromParentsToChildren": True,
    "snapshotKeyHonorModalViews": False,
}


class NoDisplayElement(ValueError):
    """A read-only hit test found no accessible element at this point."""


class TreeChannel:
    def __init__(self, rsd):
        self.rsd = rsd
        self.dtx = None
        self.pending = None

    async def start(self):
        owner = self

        class Control(DTXControlService):
            @dtx_on_invoke("hostAPIVersion")
            async def version(self):
                return 7

            @dtx_on_invoke("processDataFromRemoteDevice:")
            async def packet(self, data):
                # Only one semantic read is outstanding. Bulk updates are unsolicited.
                if owner.pending and not owner.pending[1].done():
                    kind, future = owner.pending
                    try:
                        reply = decode(data)
                        if reply.get("associatedRequestType") == kind:
                            future.set_result(reply)
                    except (ValueError, KeyError, TypeError) as exc:
                        future.set_exception(exc)

        service = self.rsd.peer_info["Services"][SERVICE]
        conn = await self.rsd.create_service_connection(int(service["Port"]))
        self.dtx = DTXConnection(conn.reader, conn.writer)
        ctrl = Control(self.dtx._control_svc._ctx)
        self.dtx._control_svc = ctrl
        self.dtx._services[0] = ctrl
        # All AccessibilityAudit messages use control channel zero. Service
        # enumeration is optional and some tvOS sessions never publish it.
        self.dtx.sent_capabilities = None
        await self.dtx.connect()
        async with asyncio.timeout(4):
            version = await self.dtx._ctrl_channel.invoke("deviceAPIVersion")
        log.info("Opened native accessibility channel, device API %s", version)
        async with asyncio.timeout(4):
            await self.dtx._ctrl_channel.invoke("clientNeedsAccessibility:", True)

    async def close(self):
        if self.dtx:
            dtx, self.dtx = self.dtx, None
            try:
                with suppress(Exception):
                    async with asyncio.timeout(1):
                        await dtx._ctrl_channel.invoke("clientNeedsAccessibility:", False)
            finally:
                await dtx.close()

    async def read(self, kind, params, token=None):
        future = asyncio.get_running_loop().create_future()
        self.pending = kind, future
        try:
            async with asyncio.timeout(8):
                await self.dtx._ctrl_channel.invoke(
                    "processDataFromHost:", request(kind, params, translation=token)
                )
                reply = await future
            if kind == 6 and reply.get("error") == 8:
                raise NoDisplayElement()
            if reply.get("error") not in (None, 0):
                raise ValueError(f"Native accessibility read failed ({reply['error']})")
            if kind == 2:
                associated = reply.get("associatedTranslationObject") or {}
                if (
                    reply.get("attribute") != 52
                    or associated.get("pid") != token["pid"]
                    or associated.get("objectID") != token["objectID"]
                ):
                    raise ValueError("Native accessibility response belongs to another snapshot")
            return reply.get("resultData")
        finally:
            self.pending = None

    async def displayed_pid(self, point):
        if not self.dtx:
            await self.start()
        hit = await self.read(6, {"point": point})
        return hit.get("pid") if isinstance(hit, dict) else None

    async def hits_row(self, row):
        frame = row.get("frame")
        if not isinstance(frame, dict) or not all(
            isinstance(frame.get(key), (float, int)) for key in ("x", "y", "width", "height")
        ) or frame["width"] <= 0 or frame["height"] <= 0:
            return False
        point = (frame["x"] + frame["width"] / 2, frame["y"] + frame["height"] / 2)
        try:
            hit = await self.read(6, {"point": point})
        except NoDisplayElement:
            return False
        return isinstance(hit, dict) and hit.get("pid") == row["pid"] and str(hit.get("objectID")) == row["uid"]

    async def snapshot(self, pid):
        if not self.dtx:
            await self.start()
        root = await self.read(1, {"pid": pid})
        if (
            not isinstance(root, dict)
            or root.get("pid") != pid
            or not root.get("isApplicationElement")
        ):
            raise ValueError("Native accessibility root did not match foreground PID")
        native = await self.read(2, {"parameter": OPTIONS}, root)
        if (
            not isinstance(native, dict)
            or native.get("UIAccessibilitySnapshotKeyElement", {}).get("objectID")
            != root["objectID"]
        ):
            raise ValueError("Native accessibility snapshot root mismatch")
        return screen_rows(native, pid)


def screen_rows(root, pid):
    application = root.get("UIAccessibilitySnapshotKeyAttributes") or {}
    pending = [(root, None, None)]
    visited = set()
    rows = {}
    focused = []
    while pending:
        node, parent_id, list_id = pending.pop()
        if not isinstance(node, dict) or id(node) in visited:
            continue
        visited.add(id(node))
        if len(visited) > 4096:
            raise ValueError("Native accessibility snapshot exceeded the node limit")
        attrs = node.get("UIAccessibilitySnapshotKeyAttributes") or {}
        token = node.get("UIAccessibilitySnapshotKeyElement") or {}
        object_id = token.get("objectID")
        # Netflix can retain a whole table behind the player. Track the owning
        # list across per-cell wrappers, or the immediate parent for plain menus.
        child_list = object_id if attrs.get(5003) in ("UITableView", "UICollectionView", "UIPickerView") else list_id
        child_parent = object_id if node is not root else None
        children = node.get("UIAccessibilitySnapshotKeyChildren") or []
        pending.extend((child, child_parent, child_list) for child in reversed(children))
        name = attrs.get(2001)
        if not isinstance(name, str) or not name.strip() or token.get("pid") != pid:
            continue
        uid = str(token["objectID"])
        traits = attrs.get(2004)
        row = {
            "uid": uid,
            "label": name,
            "caption": name,
            "pid": pid,
            "frame": attrs.get(2003),
            "selected": bool(traits & 8) if type(traits) is int and traits >= 0 else None,  # UIAccessibilityTraitSelected
            "value": attrs.get(2006),
            "bundle_id": attrs.get(3003) or application.get(3003),
            "app_name": application.get(2001),
            "source": "native_ax_snapshot",
            "picker_container": list_id if list_id is not None else parent_id,
        }
        if attrs.get(2094) == 1:
            focused.append(row)
        if attrs.get(2025) != 0 or attrs.get(2094) == 1:
            rows[uid] = row
    log.debug(
        "Native accessibility snapshot: %d labels, %d native focus matches", len(rows), len(focused)
    )
    return {
        "app": {"bundle_id": application.get(3003), "name": application.get(2001), "pid": pid},
        "items": list(rows.values()),
        "focused": focused[0] if len({r["uid"] for r in focused}) == 1 else None,
    }
