"""Public v1 MCP and REST contract."""

from ..state import Fault
from . import BOOL, KEYS, MEDIA_COMMANDS, TEXT, number, schema

UNKNOWN = {
    "power": "unknown",
    "app": None,
    "playing": None,
    "presenting": None,
    "focus": None,
    "keyboard": False,
}


def outcome(summary, evidence=(), data=None, verified=True):
    return {
        "ok": True,
        "summary": summary,
        "verified": verified,
        "evidence": list(evidence),
        "data": data or {},
    }


def checked(result):
    if not result["ok"]:
        e = result["error"]
        raise Fault(
            e["code"],
            e["message"],
            e["remediation"],
            e["retryable"],
            evidence=result.get("evidence", []),
            **({"errno": result["errno"]} if result.get("errno") is not None else {}),
        )
    return result


MAX_STEPS = 50


def described(spec, description, **annotations):
    return spec | {"description": description, **annotations}


def sequence_schema():
    target = described(TEXT, "Full accessible label. Use $item for the current for_each label.")
    check = schema({
        "focus": described(TEXT, "Exact focused label, or $item."),
        "visible": described(TEXT, "Exact label in the current accessibility items or focus, or $item."),
        "value": described({"type": "string"}, "Exact value of the focused item, or $item. An empty string is a value."),
        "app": described(TEXT, "Exact foreground app name or bundle ID from accessibility, or $item."),
    }, minProperties=1)
    steps = [
        schema({"key": described({"enum": list(KEYS)}, "Send this key once and wait for acknowledgment.")}, ["key"]),
        schema({"pause_ms": described(number(1, 5000), "Wait this many milliseconds before continuing.")}, ["pause_ms"]),
        schema({"to": target, "select": described({"type": "boolean"}, "Select after the exact label is focused.", default=False)}, ["to"]),
        schema({"text": described(TEXT, "Replace the focused field and verify keyboard readback.")}, ["text"]),
        schema({
            "app": described(TEXT, "Open this installed app by name or bundle ID."),
            "path": described({"type": "array", "items": TEXT, "minItems": 1, "maxItems": 10}, "Follow and select up to ten menu labels, as in tv_open."),
        }, ["app"]),
        schema({"expect": described(check, "Require all supplied fields to match on the same fresh screen read.")}, ["expect"]),
    ]
    return schema({
        "steps": described({"type": "array", "items": {"oneOf": steps}, "minItems": 1, "maxItems": MAX_STEPS},
                           "Ordered remote operations. At most 50 steps across all iterations."),
        "for_each": described({"type": "array", "minItems": 1, "maxItems": MAX_STEPS, "items": TEXT},
                              "Run all steps once for each listed label, in order. Substitute the whole string $item in to or expect."),
    }, ["steps"])


DIRECTIONS = ["up", "down", "left", "right"]
TV = described(TEXT, "Receiver name or ID from tv://tvs. Supply it when the target is ambiguous.")
APP = described(TEXT, "Installed app name or bundle ID from tv://apps.")
HOLD = "Hold each HID key for this many milliseconds. Media commands require zero."
KEYS_HELP = "Ordered keys, sent in one burst. The screen is read at the end and after Home. Stop on lost acknowledgment."
SHOW = {
    "url": described(TEXT, "Live HTTP(S) page on the public internet, the tailnet (Home Assistant app only; standalone blocks it unless granted) or a destination permitted by page_network_allow."),
    "html": described(TEXT, "Contents of a standalone HTML page."),
    "files": described({
        "type": "object", "required": ["index.html"],
        "properties": {"index.html": described({"type": "string"}, "Main HTML document at the root of the page.")},
        "additionalProperties": described({"type": "string"}, "File content as text or base64:-prefixed binary."),
    }, "Map relative paths to text or base64:-prefixed binary content. Include index.html at the root."),
    "bundle": described(TEXT, "Page bundle ID returned by the ZIP upload endpoint."),
    "title": described(TEXT, "Title to report in screen.presenting."),
    "audio": described(
        {"enum": ["play", "mute"]},
        "Play or mute page audio. Use mute for every page that plays no sound. It makes the mirror faster: "
        "on a Home Assistant Green it uses about one CPU core less and sends more frames, about 9 fps instead of 7 at 1080p.",
        default="play",
    ),
    "remote": described(
        {"enum": ["off", "focus", "tvos"]},
        "Send Siri Remote input to a mirrored page. The default is tvos for html, files and bundle, and focus for url. "
        "focus sends arrow keys and Enter, moves focus between links and controls and shows a focus ring. "
        "tvos is focus with a tvOS look for pages you write: the focused item lifts, up and down go straight between rows. "
        "A page that handles a key and calls preventDefault() keeps it. off sends no input. "
        "Clickpad remote: ring clicks are arrows, center click is Enter. "
        "Touch-surface remote: swipes and flicks are arrows, any click is Enter. "
        "Menu and Home stay with tvOS and end the mirror. Play/Pause and volume are not forwarded.",
    ),
}


def canonical(name, args):
    """Return args with undocumented aliases replaced. Call it before schema validation.

    Earlier callers sent remote: "keys". The schema does not list it. focus sends the same keys,
    and a page that handles a key and calls preventDefault() keeps it.
    """
    if name == "tv_show" and isinstance(args, dict) and args.get("remote") == "keys":
        return args | {"remote": "focus"}
    return args


# These are caller-side examples. The browser check runs the advertised recipes verbatim.
OUTLINE_RECIPE = """const roles = {a:'link', button:'button', text:'textbox', password:'textbox', search:'searchbox', h1:'heading', h2:'heading', h3:'heading', h4:'heading', h5:'heading', h6:'heading'};
const items = [...document.querySelectorAll('button,a[href],input,h1,h2,h3,h4,h5,h6')];
return items.filter(el => el.checkVisibility({visibilityProperty: true}) && !el.closest('[inert],[aria-hidden=true]')).slice(0, 40)
  .map(el => ({role: el.getAttribute('role') || roles[el.localName === 'input' ? el.type : el.localName] || el.type, text: (el.getAttribute('aria-label') || el.innerText).trim().slice(0, 100),
    value: el.matches('input[type=checkbox],input[type=radio]') ? el.checked : undefined}));"""
FILL_RECIPE = """const el = document.querySelector(arg.selector);
const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, arg.text);
for (const type of ['input', 'change']) el.dispatchEvent(new Event(type, {bubbles: true}));"""
SELECT_RECIPE = """const el = document.querySelector('#size');
el.value = arg;
el.dispatchEvent(new Event('change', {bubbles: true}));"""
WAIT_RECIPE = """const end = performance.now() + 1500;
while (document.querySelector('#result')?.textContent !== 'Done') {
  if (performance.now() >= end) throw new Error('Status did not reach Done');
  await new Promise(resolve => setTimeout(resolve, 25));
}
return 'Done';"""
RUN_HELP = f"""Run JavaScript in the current mirrored document. Supply an expression, function or async function body. Pass JSON input as arg.
Call functions on your own page directly. Read a known value with document.querySelector('#note').value.
For discovery, narrow the selector when possible. This outline returns common roles or input types and omits free-text input values:
{OUTLINE_RECIPE}
Fill a controlled input or textarea using arg.selector and arg.text:
{FILL_RECIPE}
Select an option:
{SELECT_RECIPE}
Click with document.querySelector('#advance').click(). Submit with document.querySelector('form').requestSubmit().
Wait for a status change:
{WAIT_RECIPE}
The next call follows navigation. Promises are awaited. Read data.result, or data.preview when data.truncated is true.
Set page_scripts to false to disable this tool. For example, tv_page_run {{"script":"document.querySelector('#status').innerText"}}."""

SCHEMAS = {
    "tv_show": schema(
        SHOW,
        oneOf=[
            {"maxProperties": 0},
            *[{"required": [key]} for key in ("url", "html", "files", "bundle")],
        ],
    ),
    "tv_page_run": schema({
        "script": described(TEXT, "JavaScript expression, function expression or async function body. arg is available in the page.", maxLength=65536),
        "arg": described({}, "Optional JSON input to the script."),
        "timeout_ms": described(number(1, 30000), "Maximum wait in milliseconds.", default=5000),
    }, ["script"]),
    "tv_play": schema(
        {
            "youtube": described(TEXT, "YouTube video URL or 11-character video ID."),
            "media": described(TEXT, "Direct HTTP(S) media URL the Apple TV can fetch over AirPlay."),
            "link": described(TEXT, "Web or custom-scheme deep link to open and inspect in the target app."),
            "title": described(TEXT, "Title to find and play in the specified app."),
            "app": APP,
        },
        oneOf=[{"required": [key]} for key in ("youtube", "media", "link")]
        + [{"required": ["app", "title"]}],
        dependentRequired={"title": ["app"], "app": ["title"]},
    ),
    "tv_open": schema(
        {"app": APP,
         "search": described(TEXT, "Search query to enter or launch in the app. Choose search or path."),
         "path": described({"type": "array", "items": TEXT, "minItems": 1}, "Menu labels to focus and select in order. Settings starts at its root.")},
        ["app"],
        **{"not": {"required": ["search", "path"]}},
    ),
    "tv_look": schema({
        "image": described(BOOL, "Also capture a PNG. tvOS shows a screenshot toast.", default=False),
        "max_width": described(number(1, 4096), "Maximum PNG width in pixels when image is true.", default=1280),
    }),
    "tv_navigate": schema({
        "to": described(TEXT, "Accessible label to focus. Matching accepts case-insensitive prefixes."),
        "within": described(TEXT, "Prefer matching items in this accessibility container if available."),
        "select": described(BOOL, "Press Select once the target has focus.", default=False),
        "ensure_selected": described(BOOL, "Ensure one exact visible menu option has its selected trait. Skip an already selected option.", default=False),
    }, ["to"], **{"not": {"required": ["select", "ensure_selected"],
                          "properties": {"select": described({"const": True}, "Activate the focused row."),
                                         "ensure_selected": described({"const": True}, "Verify the selected option.")}}}),
    "tv_remote": schema(
        {
            "keys": described({"type": "array", "minItems": 1, "maxItems": 50, "items": {"enum": list(KEYS)}}, KEYS_HELP),
            "hold_ms": described(number(0, 10000), HOLD, default=0),
            "text": described(TEXT, "Replace the contents of the focused keyboard field and check readback."),
            "submit": described(BOOL, "Press Select after successful text entry.", default=False),
            "swipe": described({"enum": DIRECTIONS}, "Send one directional swipe."),
            "check_each": described(BOOL, "Read the screen after every key, report whether each changed it, and stop if focus becomes unreadable. Slower.", default=False),
        },
        oneOf=[{"required": [key]} for key in ("keys", "text", "swipe")],
        dependentRequired={"hold_ms": ["keys"], "submit": ["text"], "check_each": ["keys"]},
        **{
            "if": {"required": ["hold_ms"], "properties": {"hold_ms": described({"exclusiveMinimum": 0}, HOLD)}},
            "then": {"properties": {"keys": described({"items": {"not": {"enum": list(MEDIA_COMMANDS)}}}, KEYS_HELP)}},
        },
    ),
    "tv_seek": schema({
        "position_s": described({"type": "integer", "minimum": 0}, "Absolute position in whole seconds."),
        "offset_s": described({"type": "integer", "not": {"const": 0}}, "Signed skip in whole seconds. Negative moves backward."),
    }, oneOf=[{"required": ["position_s"]}, {"required": ["offset_s"]}]),
    "tv_volume": schema({"percent": described({"type": "number", "minimum": 0, "maximum": 100},
                          "Absolute volume of the current output, in percent.")}, ["percent"]),
    "tv_sequence": sequence_schema(),
    "tv_power": schema({"on": described(BOOL, "True to wake, false to sleep.")}, ["on"]),
    "tv_pair": schema({
        "kind": described({"enum": ["control", "screen"]}, "Pair remote control and AirPlay, or pair the accessibility and screenshot service."),
        "code": described({"type": "string", "pattern": "^[0-9]{4}([0-9]{2})?$"}, "On-screen PIN as text, including leading zeros. Control uses four digits and screen uses six."),
        "pairing_id": described(TEXT, "Exchange ID returned on start. Use it to poll, submit or cancel the same exchange."),
        "read_code_from_screen": described(BOOL, "With user approval and working screen pairing, try to read and submit the control PIN.", default=False),
        "notify": described(BOOL, "Request a reply notification on the phone configured in HA.", default=False),
        "cancel": described(BOOL, "Close the pending exchange for this kind.", default=False),
    }, ["kind"]),
    "tv_status": schema(),
    "tv_history": schema({
        "since": described(TEXT | {"maxLength": 40}, "Start of the time range. ISO 8601 date or date-time in UTC, or an age such as 24h, 7d or 4w."),
        "until": described(TEXT | {"maxLength": 40}, "End of the time range, in the same formats as since."),
        "app": described(TEXT, "Only entries from this app. Matches part of the app name or the whole bundle ID, ignoring case."),
        "search": described(TEXT, "Only entries whose title, series, artist or album contains this text, ignoring case."),
        "limit": described(number(0, 500), "Maximum entries to return, newest first. Use 0 for the summary alone.", default=50),
    }),
}
# These tools read bridge state and return no screen.
NO_SCREEN = ("tv_status", "tv_history")
# The dispatcher consumes routing fields before invoking the per-TV operation.
ROUTED_SCHEMAS = {
    name: spec | {"properties": spec["properties"] | {"tv": TV}} for name, spec in SCHEMAS.items()
}
ROUTED_SCHEMAS["tv_show"] = ROUTED_SCHEMAS["tv_show"] | {
    "properties": ROUTED_SCHEMAS["tv_show"]["properties"] | {
        "replace": described(BOOL, "When mirror slots are full, stop the oldest holder's presentation to make room.", default=False),
    },
    "oneOf": [
        {"properties": {"tv": TV}, "additionalProperties": False},
        *SCHEMAS["tv_show"]["oneOf"][1:],
    ],
}
INSTRUCTIONS = (
    "Choose a receiver from tv://tvs. Supply its name or ID in tv when there is no default. "
    "Use tv_status for health and pairing, and tv_pair to repair missing access. "
    "Read tv_look before interrupting someone. Use tv_history to learn what people watched and for how long. Use tv_open to launch or search, tv_navigate for a labeled item, "
    "tv_remote for individual keys, text or a swipe, and tv_sequence for repeated work with explicit screen checkpoints. "
    "Use tv_seek for absolute or relative seeking and tv_volume to set the current output level. "
    "Use tv_power for wake or sleep, tv_play for media and deep links, and tv_show for web pages. "
    "Use tv_page_run to call app functions or read and operate the mirrored document with DOM APIs. "
    "TV actions can wake the receiver and dismiss a screen saver or profile prompt. Inspect verified, evidence and screen. "
    "After uncertain delivery, look before sending further input. Ask before purchases, rentals or upgrades."
)
PAGE_GUIDE = """Design for a 1920×1080 canvas with body text of at least 28 px. Put index.html at the root of
inline files or an uploaded ZIP. Use the base64: prefix for binary inline files. On authored pages,
expose named functions on window and call them with tv_page_run.
For live URLs, keep the server running. The Green loads the real site, including scripts, fetches,
WebSockets and reloads. For a local server, use tailscale serve and pass its HTTPS MagicDNS URL.
Pages that block framing open at the top level. Bundle-relative links and assets work.
Page requests to Home Assistant and other LAN services are blocked by default.
Page audio plays by default. Use audio: "mute" for every page that plays no sound. On a Home Assistant
Green the mirror then uses about one CPU core less and sends about 9 fps instead of 7 at 1080p.
The Siri Remote works on a mirrored page by default: remote is "tvos" for html, files and bundle,
and "focus" for url. Set remote: "off" to send no input. With remote: "focus", a mirrored page gets
ArrowLeft, ArrowRight, ArrowUp, ArrowDown and Enter key events, and arrows move focus between links and
controls, unless the page handles the key and calls preventDefault().
With remote: "tvos", the focused item also lifts with a shadow and a white ring, and Up and Down go to
the next row on screen and take the item straight above or below, or else the nearest one. For "tvos", put focusable items in rows and grids, put
each row in a horizontal scroll container or a data-tv-group element, leave 8% spare room around
items, use a dark background, and do not use hover-only controls or animation that runs without input.
Clickpad remote: ring clicks are arrows and the center click is Enter (verified).
Touch-surface remote: swipes are arrows, one per step of finger travel, a brisk swipe or a flick adds up to four more,
and any click is Enter (clicks verified, swipe tuning preliminary).
Menu and Home stay with tvOS and end the mirror. The bridge then dismisses the page and does not show it
again, and later results report screen.last_presentation_end. That always works when the TV closes the
video connection. When it closes only the control connection, the bridge must see the press, which needs
screen.presenting.remote.state "active". Otherwise the page can return about a second later.
Play/Pause and volume are not forwarded."""
RETRYABLE = {"tv_unreachable", "page_unreachable", "screen_unavailable", "busy"}
CODES = {
    "wake_failed": "tv_unreachable",
    "unreachable": "tv_unreachable",
    "pairing_required": "not_paired",
    "allow_access_changed": "access_blocked",
    "invalid_pin": "not_paired",
    "invalid_pairing_id": "invalid_arguments",
    "command_rejected": "launch_refused",
    "app_not_found": "app_not_installed",
    "keyboard_unfocused": "no_text_field",
    "fork_required": "launch_refused",
    "bundle_missing": "page_unreachable",
    "stage_timeout": "page_unreachable",
    "sender_failed": "page_unreachable",
    "invalid_bundle": "invalid_arguments",
    "content_full": "page_unreachable",
    "observer_failed": "screen_unavailable",
    "screen_pairing_invalid": "screen_unavailable",
    "observer_unavailable": "screen_unavailable",
    "delivery_uncertain": "uncertain",
    "open_unverified": "uncertain",
    "playback_unverified": "uncertain",
}
STABLE = RETRYABLE | {
    "blocked_address",
    "scripts_disabled", "script_error", "stale_view", "target_missing", "page_not_live", "nothing_showing",
    "launch_refused", "tv_required", "mirror_busy", "tv_not_found",
    "not_paired",
    "pairing_removed",
    "access_blocked",
    "app_not_installed",
    "not_found",
    "no_text_field",
    "unsupported_action",
    "uncertain",
    "invalid_arguments",
}


def public_error(fault):
    code = CODES.get(fault.code, fault.code)
    if code not in STABLE:
        code = "uncertain"
    return {
        "code": code,
        "message": str(fault),
        "retryable": code in RETRYABLE,
        "fix": fault.remediation,
    }
