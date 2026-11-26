# SPDX-License-Identifier: GPL-3.0-or-later
"""Passive accessibility reads from the tvOS audit daemon."""

from .native import SERVICE, NoDisplayElement, TreeChannel

# Logical points on the 1920 × 1080 tvOS canvas. In Settings the shell owns
# the space between the sidebar and the rows. A single central hit misses the app.
DISPLAY_POINTS = ((1440, 540), (960, 540), (480, 540), (1440, 270), (960, 810))


class ScreenReader:
    def __init__(self, audit, rsd):
        self.audit = audit
        services = getattr(rsd, "peer_info", {}).get("Services", {})
        self.tree = TreeChannel(rsd) if SERVICE in services else None

    async def start(self):
        if self.tree:
            return
        await self.audit.set_show_visuals(False)
        await self.audit._invoke("deviceEnableHighlight:", False, expects_reply=False)

    async def close(self):
        if self.tree:
            await self.tree.close()

    async def displayed_screen(self):
        visited = set()
        partial = None
        for point in DISPLAY_POINTS:
            try:
                pid = await self.tree.displayed_pid(point)
            except NoDisplayElement:
                continue
            if not pid or pid in visited:
                continue
            visited.add(pid)
            screen = await self.tree.snapshot(pid)
            if screen["focused"]:
                return screen
            if screen["items"] and (
                partial is None or len(screen["items"]) > len(partial["items"])
            ):
                partial = screen
        return partial or {"app": None, "items": [], "focused": None}

    async def observe(self, maximum, menu_label=None):
        if self.tree:
            screen = await self.displayed_screen()
            app, items, focus = screen["app"], screen["items"], screen["focused"]
        else:
            app, focus, items = None, None, []
            root = await self.audit._invoke("deviceFetchSpecialElement:", 0)
            if root:
                hierarchy = await self.audit._invoke(
                    "deviceElement:valueForAttribute:",
                    root,
                    self.audit._serialize_attribute("_AXHierarchyElementsAttribute"),
                )
                items = list(tree_items(hierarchy))
        limited = items[:maximum]
        if focus and all(item["uid"] != focus["uid"] for item in limited):
            limited = [*limited[: maximum - 1], focus]
        if menu_label and self.tree and len(items) <= maximum:
            wanted = " ".join(menu_label.casefold().split())
            matches = [row for row in limited if " ".join(row["label"].casefold().split()) == wanted]
            if len(matches) == 1:
                row = matches[0]
                container = row.get("picker_container")
                same_picker = focus and focus.get("pid") == row["pid"] and (
                    focus.get("uid") == row["uid"]
                    or (container is not None and container == focus.get("picker_container"))
                )
                row["on_screen"] = bool(same_picker and await self.tree.hits_row(row))
        return {"app": app, "items": limited, "focused": focus, "truncated": len(items) > maximum}


def tree_items(tree):
    pending = [tree]
    while pending:
        node = unarchive(pending.pop())
        if not isinstance(node, dict):
            continue
        children = unarchive(node.get("ChildrenValue_v1"))
        if isinstance(children, list):
            pending.extend(reversed(children))
        el = unarchive(node.get("AuditElementValue_v1")) or {}
        ident = unarchive(el.get("PlatformElementValue_v1"))
        desc = unarchive(node.get("HumanReadableDescriptionValue_v1"))
        role = unarchive(node.get("HumanReadableRoleDescriptionValue_v1"))
        if isinstance(ident, bytes) and isinstance(desc, str) and desc:
            yield {"uid": ident.hex(), "caption": desc, "spoken_description": desc, "role": role}


def unarchive(value):
    # AXAuditApplication_v1 is missing from the SDK deserializer's class map.
    while isinstance(value, dict) and value.get("ObjectType") in (
        "passthrough",
        "AXAuditApplication_v1",
        "AXAuditNode_v1",
        "AXAuditElement_v1",
    ):
        value = value.get("Value")
    return value


async def running_apps(audit):
    records = unarchive(await audit._invoke("deviceRunningApplications"))
    apps = []
    for entry in records or []:
        record = unarchive(entry)
        if not isinstance(record, dict):
            continue
        apps.append(
            {
                "bundle_id": unarchive(record.get("BundleIdentifierValue_v1")),
                "name": unarchive(record.get("DisplayNameValue_v1")),
                "pid": unarchive(record.get("PidValue_v1")),
                **(
                    {"foreground": True}
                    if any(
                        unarchive(record.get(key)) is True
                        for key in ("IsForegroundValue_v1", "IsFrontmostValue_v1")
                    )
                    else {}
                ),
            }
        )
    return {"apps": apps}
