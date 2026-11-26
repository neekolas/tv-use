---
name: tv-use-connect
description: Connect to an Apple TV through the tv-use MCP server on Home Assistant and control it. Covers local or remote connection, reading the screen, the remote, apps, deep links, playback, power and error recovery. Use when an agent needs to reach or operate the TV, or a tv_* call fails.
---

# Connect to and control the TV

tv-use exposes every paired Apple TV in the home through one MCP endpoint on Home Assistant: `<HA origin>/api/atv_bridge/mcp`, Streamable HTTP, header `Authorization: Bearer <HA admin token>`.

## Connect

- **On the home network:** use `http://homeassistant.local:<port>` or HA's LAN IP. Fastest; no Tailscale.
- **Outside the home:** use HA's tailnet hostname through Tailscale.

- **Standalone bridge (no Home Assistant):** use `http://127.0.0.1:8765/mcp` with its own token; REST is `POST /api/tool/<name>`.

Same token for both HA paths. The port is 8123 by default, 80 on some installs. If the tools are missing, or you are at home on a tailnet URL, see [connect.md](references/connect.md).

## First calls

1. `tv_status {}` or `tv://tvs` lists the TVs. Every tool takes an optional `tv` (name or stable ID); with several TVs, always pass it.
2. Before changing a screen someone may be watching, read it: `tv_look {"tv":"<room>","image":false}`.

## Tools

| Goal | Tool |
|---|---|
| Read app, focus, labels, playback | `tv_look`; `"image":true` adds a screenshot and a toast on the TV |
| Ensure a visible picker option is checked | `tv_navigate {"to":"English (CC)","ensure_selected":true}` |
| Select a visible label | `tv_navigate {"to":"General","select":true}` |
| Keys, swipe, text | `tv_remote`; one burst, `"check_each":true` only when every press must be confirmed |
| A known route with checks | `tv_sequence` |
| Open an app, search, follow a menu path | `tv_open` |
| YouTube, media URL, deep link | `tv_play`; the TV fetches media URLs itself |
| Seek within playback | `tv_seek` with `position_s` or a signed `offset_s` in whole seconds |
| Stop playback | `tv_remote {"keys":["stop"]}` |
| Set volume | `tv_volume {"percent":25}` on an output that supports absolute volume |
| Wake or sleep | `tv_power` |
| What the household watched, or what's playing now without a screen read | `tv_history {"since":"30d","limit":0}` for a summary, then narrow with `app`, `search` or `tv`. `watched_s` is seconds actually played; Netflix gives no titles; `data.now` is what's playing. |
| Show and change a web page | `tv_show`, `tv_page_run`; see **tv-use-web-apps** |
| Repair pairing | `tv_pair` |

Results carry `ok`, `summary`, `verified`, `evidence` and usually `screen`. `verified: false` means delivered but unconfirmed: look before sending more. On failure, read `error.fix` first.

## Rules

- After a media or deep-link launch, your next call is a text `tv_look`. Handle any **Open in …?** prompt before another key or launch.
- Ask before purchases, rentals, upgrades or sign-in. There is no purchase guard.
- On `mirror_busy`, ask before `replace: true`.
- Watch history is private household data. Never send it to an outside service unless the user asks.

## References

| File | Load to |
|---|---|
| [connect.md](references/connect.md) | Register the server, probe for a local connection, call tools over REST |
| [native-apps.md](references/native-apps.md) | Verify checked options, run sequences, handle profiles and Open prompts, control power |
| [deeplinks.md](references/deeplinks.md) | Find a deep link for a streaming service |
| [examples.md](references/examples.md) | Play a podcast, find which app has a show, script a Settings route |
| [troubleshooting.md](references/troubleshooting.md) | Handle an error code, status problem, HTTP error or pairing exchange |
| [MCP API](https://github.com/neekolas/tv-use/blob/main/docs/mcp-api.md) | Check every argument and result field |
