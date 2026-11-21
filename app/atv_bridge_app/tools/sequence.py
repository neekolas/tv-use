"""Bounded remote sequences, executed inside the receiver's public tool lock."""

import asyncio

from ..state import Fault, classify
from . import invalid
from .contract import MAX_STEPS, UNKNOWN, checked, outcome, public_error
from .navigation import label, normal

TIME_LIMIT = 90
SCREEN_TIMEOUT = 5
ITEM = "$item"


def expand(steps, for_each):
    labels = for_each if for_each is not None else [None]
    if len(steps) * len(labels) > MAX_STEPS:
        raise invalid("A sequence can run at most 50 steps after repetition.")
    expanded = []

    def bind(value, item):
        if value != ITEM:
            return value
        if item is None:
            raise invalid("$item needs a for_each list.")
        return item

    for iteration, item in enumerate(labels):
        for index, step in enumerate(steps):
            if "to" in step:
                step = step | {"to": bind(step["to"], item)}
            elif "expect" in step:
                step = {"expect": {k: bind(v, item) for k, v in step["expect"].items()}}
            expanded.append(({"iteration": iteration, "step": index}, step))
    return expanded


async def checkpoint(tools, expected):
    view = await tools.read_observation(single_read=True)
    focus = view["focused"]
    screen = await tools.snapshot(focus, False, view)
    app = screen["app"] or {}
    visible = [label(i) for i in view["items"]]
    if focus:
        visible.append(label(focus))
    actual = {
        "focus": label(focus) or None,
        "value": (focus or {}).get("value"),
        "visible": visible,
        "app": [app.get("id"), app.get("name")]
        if app.get("source") == "accessibility"
        else [],
    }
    for field, wanted in expected.items():
        observed = actual[field]
        candidates = observed if isinstance(observed, list) else [observed]
        if not any(
            isinstance(v, str) and normal(v) == normal(wanted) for v in candidates
        ):
            raise Fault(
                "not_found",
                f"Checkpoint did not match {field}.",
                "Inspect the observed value and screen before continuing.",
                field=field,
                expected=wanted,
                observed=observed,
            )
    return outcome(
        "Checkpoint matched.",
        ["Fresh accessibility observation"],
        {"matched": expected},
    ) | {"screen": screen}


async def run_step(tools, step):
    if "key" in step:
        await tools.press(step["key"])
        return outcome(
            "Key acknowledged.",
            ["Visible effect unchecked"],
            {"key": step["key"]},
            verified=False,
        )
    if "pause_ms" in step:
        await asyncio.sleep(step["pause_ms"] / 1000)
        return outcome("Pause completed.")
    if "expect" in step:
        return await checkpoint(tools, step["expect"])
    if "text" in step:
        checked(await tools.ops.tv_type(step["text"]))
        return outcome("Text matched keyboard readback.", ["Keyboard readback"])
    if "to" in step:
        return await tools.tv_navigate(**step, exact=True)
    return await tools.tv_open(**step, require_verified_launch=True)


async def run_sequence(tools, steps, for_each):
    # Semantic limits precede readiness, which itself can wake the TV or press Menu.
    try:
        expanded = expand(steps, for_each)
    except Fault as fault:
        return tools.failure("tv_sequence", fault)
    report = {"steps": [], "total_steps": len(expanded), "stopped_at": None}
    where = {"phase": "prepare"}
    row = None
    failure = None
    screen = None
    try:
        async with asyncio.timeout(TIME_LIMIT):
            await tools.prepare_action()
            for location, step in expanded:
                where = location
                screen = None
                op = next(
                    k
                    for k in ("expect", "key", "pause_ms", "to", "text", "app")
                    if k in step
                )
                row = location | {"operation": op}
                report["steps"].append(row)
                result = await run_step(tools, step)
                screen = result.pop("screen", None)
                row.update(result)
                if not result["verified"] and op != "key":
                    raise Fault(
                        "uncertain",
                        result["summary"] + " Effect unconfirmed.",
                        "Inspect the screen before sending another action.",
                    )
    except Exception as exc:
        fault = (
            Fault(
                "uncertain",
                "Sequence timed out. An in-flight action may have arrived.",
                "Read tv_look before sending more input.",
            )
            if isinstance(exc, TimeoutError)
            else classify(exc)
        )
        failure = public_error(fault)
        report["stopped_at"] = where
        if row is not None:
            row.update(
                ok=False, verified=False, error=failure, summary=failure["message"]
            )
            row.setdefault("evidence", fault.fields.get("evidence", []))
            row.setdefault(
                "data", {k: v for k, v in fault.fields.items() if k != "evidence"}
            )
            if row["operation"] == "expect" and fault.code == "not_found":
                screen = tools.cached_screen()

    if screen is None:
        try:
            async with asyncio.timeout(SCREEN_TIMEOUT):
                screen = await tools.snapshot()
        except Exception:
            screen = UNKNOWN.copy()
            if failure is None:
                fault = Fault(
                    "screen_unavailable",
                    "Could not read the final screen.",
                    "Read tv_look before continuing.",
                )
                failure = public_error(fault)
                report["stopped_at"] = {"phase": "screen"}
    if failure:
        return outcome(failure["message"], data=report, verified=False) | {
            "ok": False,
            "error": failure,
            "screen": screen,
        }
    verified = all(r["verified"] for r in report["steps"])
    return outcome(
        "Sequence completed.",
        ["See data.steps for delivery and verification."],
        report,
        verified,
    ) | {"screen": screen}
