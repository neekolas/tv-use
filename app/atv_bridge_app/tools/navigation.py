import asyncio
from difflib import SequenceMatcher
from ..state import Fault

def normal(text):
    return " ".join((text or "").casefold().split())


def label(item):
    return (item or {}).get("label") or (item or {}).get("caption") or ""


def match_app(query, installed):
    query = normal(query)
    if not query:
        raise Fault("invalid_arguments", "An app name is required.", "Supply a name or bundle ID.")
    ranked = []
    for app in installed:
        names = [normal(app["bundle_id"]), normal(app["name"])]
        score = max(
            1 if query == n else 0.9 if query in n else SequenceMatcher(None, query, n).ratio()
            for n in names
        )
        if score >= 0.6:
            ranked.append((score, app))
    ranked.sort(key=lambda pair: -pair[0])
    candidates = [app for score, app in ranked if ranked[0][0] - score < 0.05]
    if len(candidates) != 1:
        raise Fault(
            "app_not_installed",
            "The installed app name has no unique match.",
            "Use an installed bundle ID from tv://apps.",
            apps=[{"id": a["bundle_id"], "name": a["name"]} for a in candidates],
        )
    return candidates[0]


def center(item):
    frame = (item or {}).get("frame")
    if isinstance(frame, dict):
        frame = [frame.get(k) for k in ("x", "y", "width", "height")]
    if (
        isinstance(frame, (tuple, list))
        and len(frame) == 4
        and all(isinstance(n, (int, float)) for n in frame)
    ):
        x, y, width, height = frame
        return x + width / 2, y + height / 2
    return None


def matches_label(item, target, exact):
    current, wanted = normal(label(item)), normal(target)
    return current == wanted if exact else current.startswith(wanted)


def direction_to(focus, items, destination, alternate):
    here = next(
        (i for i in items if (focus or {}).get("uid") and i.get("uid") == focus["uid"]), None
    ) or next((i for i in items if normal(label(i)) == normal(label(focus))), focus)
    a, b = center(here), center(destination)
    if a and b and a != b:
        dx, dy = b[0] - a[0], b[1] - a[1]
        direction = (
            ("right" if dx > 0 else "left")
            if dx and (not dy or (abs(dx) > abs(dy)) != alternate)
            else ("down" if dy > 0 else "up")
        )
    else:
        backward = (
            here in items and destination in items and items.index(destination) < items.index(here)
        )
        direction = (
            ("left" if alternate else "up") if backward else ("right" if alternate else "down")
        )
    return direction


async def navigate(observe, press, to, select=False, within=None, *, exact=False):
    target, path = normal(to), []
    if not target:
        raise Fault(
            "invalid_arguments", "A target label is required.", "Supply the label from tv_look."
        )
    stalled = 0
    alternate = False
    focus, items = await observe()
    while True:
        if not focus or not label(focus):
            raise Fault(
                "screen_unavailable",
                "The current focus label is unavailable.",
                "Inspect tv_look before sending a direction key.",
                path=path,
            )
        current = normal(label(focus))
        matches = [i for i in items if matches_label(i, target, exact)]
        if within:
            scoped = [
                i for i in matches if normal(within) in normal(i.get("within") or i.get("container"))
            ]
            if scoped:
                matches = scoped
        destination = next(
            (i for i in matches if normal(label(i)) == target), next(iter(matches), None)
        )
        at_target = matches_label(focus, target, exact) and (
            not destination
            or not focus.get("uid")
            or not destination.get("uid")
            or focus["uid"] == destination["uid"]
        )
        if at_target:
            if select:
                if len(path) >= 40:
                    raise Fault(
                        "not_found",
                        "The navigation press limit was reached.",
                        "Inspect the focused label.",
                        path=path,
                    )
                try:
                    await press("select")
                except Fault as exc:
                    exc.fields["path"] = [*path, "select"]
                    raise
                path.append("select")
            return path
        if len(path) >= 40 or stalled >= 3:
            raise Fault(
                "not_found",
                f"Focus did not reach {to}.",
                "Inspect tv_look and choose another label.",
                path=path,
            )
        direction = direction_to(focus, items, destination, alternate)
        try:
            await press(direction)
        except Fault as exc:
            exc.fields["path"] = [*path, direction]
            raise
        path.append(direction)
        previous = (current, (focus or {}).get("uid"))
        focus, items = await observe()
        unchanged = previous == (normal(label(focus)), (focus or {}).get("uid"))
        stalled = stalled + 1 if unchanged else 0
        if unchanged:
            alternate = not alternate


def row_id(row):
    if row and row.get("pid") is not None and row.get("uid") is not None:
        return row["pid"], row["uid"]
    return None


def menu_option(observation, to, within):
    if observation.get("truncated"):
        return None
    matches = [
        row for row in observation["items"]
        if matches_label(row, to, exact=True)
        and (not within or normal(row.get("within") or row.get("container")) == normal(within))
    ]
    return matches[0] if len(matches) == 1 and matches[0].get("on_screen") is True else None


async def select_option(read, press, to, within, path):
    """Return the checkmark and its observation. The caller retains the attempted input path."""
    if not normal(to):
        raise Fault("invalid_arguments", "A menu option label is required.", "Supply its full label.")
    observation = await read(single_read=True, menu_label=to)
    target_id = row_id(menu_option(observation, to, within))
    previous_focus = None
    stalled = 0
    while True:
        option = menu_option(observation, to, within)
        if target_id is None or row_id(option) != target_id:
            raise Fault("not_found", "Could not identify the same unique visible menu option.",
                        "Read tv_look and check the exact label and container.")
        if option.get("selected") is True:
            return True, observation
        if option.get("selected") is not False:
            raise Fault("screen_unavailable", "The option's selection state is unavailable.",
                        "Inspect the app's picker before acting.")
        focus = observation["focused"]
        current = row_id(focus)
        if current is None or current[0] != target_id[0]:
            raise Fault("screen_unavailable", "The picker has no identifiable focused row.",
                        "Read tv_look before sending input.")
        if current == previous_focus:
            stalled += 1
        else:
            stalled = 0
        if len(path) >= 40 or stalled >= 3:
            raise Fault("not_found", "Focus did not reach the menu option.", "Inspect the picker.")
        if current == target_id and matches_label(focus, to, exact=True):
            if previous_focus == current:
                path.append("select")
                await press("select")
                break
            # A second fresh snapshot guards a focus or checkmark change just before activation.
        else:
            direction = direction_to(focus, observation["items"], option, stalled % 2)
            path.append(direction)
            await press(direction)
        previous_focus = current
        observation = await read(single_read=True, menu_label=to)

    for attempt in range(3):
        if attempt:
            await asyncio.sleep(0.3)
        try:
            observation = await read(single_read=True, menu_label=to)
        except Fault:
            observation = {"app": None, "focused": None, "items": []}
        option = menu_option(observation, to, within)
        if row_id(option) == target_id and option.get("selected") is True:
            return True, observation
    return False, observation
