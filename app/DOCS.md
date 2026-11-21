# Apple TV Agent Bridge

The bridge lets Home Assistant control Apple TVs by room, capture their screens and show web pages with audio. Agents use Home Assistant's authenticated endpoints.

## Install on Home Assistant OS

Use an Apple TV on tvOS 27 or later and a Home Assistant OS host on the same LAN. The app supports aarch64 and amd64. Testing included tvOS 27.2 (24K122) on AppleTV11,1. tvOS 27 or later is recommended and tested. tvOS 26.6 also works but is less tested (pairing, control and screen reading worked on an Apple TV 4K 1st generation). You do not need an Apple Developer account.

On a workstation with this checkout, install `uv`, `rsync` and OpenSSH. In your HA administrator profile, create a long-lived access token under **Security**. Save it as one line in `~/.config/atv-bridge/ha_token` and set the file mode to `600`. Set `HA_URL` to the origin you use to reach HA, including its port. The workstation must also be able to reach SSH and port 8766 on that host.

From the repository root, run these commands in order. `push-app` stages the fork, installs or rebuilds the app, starts it and checks its watchdog. `push-integration` replaces the integration files and restarts HA Core.

```sh
export HA_URL='http://<ha-host>:8123'  # Replace with your HA origin.
uv run tools/deploy/deploy.py --url "$HA_URL" check
uv run tools/deploy/deploy.py --url "$HA_URL" ssh-setup
uv run tools/deploy/deploy.py --url "$HA_URL" push-app
uv run tools/deploy/deploy.py --url "$HA_URL" push-integration
```

If the discovery prompt is missing after the Core restart, run `uv run tools/deploy/deploy.py --url "$HA_URL" restart-app` to announce the app again. [Deployment details](../docs/deploy.md)

## Pair a receiver

1. Leave **apple_tv_host** empty for automatic discovery, or enter a fixed IPv4 address if you use a DHCP reservation. Existing configured addresses continue to work.
2. If you are replacing a built-in Apple TV entry, disable that receiver’s old entry in **Settings → Devices & services** when you switch to the bridge.
3. Enable AirPlay on the TV. Under **Settings → AirPlay and Apple Home → Allow Access**, select **Anyone on the Same Network**.
4. Start the app. With the companion custom integration installed, open **Settings → Devices & services** and configure the discovered Apple TV Agent Bridge.
5. In discovery mode, select your Apple TV by name and address. Enter the four-digit AirPlay PIN shown on that TV. The bridge derives Companion, RAOP and tvpipe credentials from this pairing. If Companion needs its own pairing, complete the second PIN prompt in Home Assistant.
6. For accessibility reads and screenshots, open **Settings → Remotes and Devices → Remote App and Devices** on the Apple TV, then enter the six-digit Remote Pairing PIN in Home Assistant. On tvOS 27.2 the observer uses a userspace tunnel and can capture screenshots without a developer disk image.

The controller sends the name **Agent Bridge** during pairing and control. The app preserves its signing key and controller ID across updates. Keep the app data when updating. Use the integration's configuration flow to pair again after removing a controller on the TV. The **log_level** option accepts `debug`, `info`, `warning` and `error`.

Both **apple_tv_host** and **apple_tv_id** default to empty. Pairing saves the selected TV’s stable identifier, advertised identifiers, name, model and last known IPv4 address in app data. You can also set **apple_tv_id** to an advertised identifier before pairing. Leave it empty to use the saved selection.

The bridge tries the configured address first, then the last known address, then Bonjour. If the TV changes all advertised identifiers, the bridge can use a unique exact name match and logs a warning. After authentication, it remembers the new identifiers and address. `tv_status` reports the address and its source. An informational `address_changed` problem shows the previous and new addresses.

## Multiple TVs

Use **Add Apple TV** on the existing HA entry. Pick the room and enter its control PIN. Choose **Skip screen pairing** to finish with control only, or enter the screen PIN for accessibility and screenshots. Each room gets a device and entities. Use **Configure** to pair the screen later or repair either pairing. Removing a room’s HA subentry, or removing its last configured address or ID from the app options and restarting the app, erases the bridge’s saved state for that room. Adding it again requires pairing.

The `apple_tvs` option accepts a list with optional `name`, `host` (IPv4), and `id` (advertised identifier). Supply at least one field per row. With an empty list, HA lets you choose discovered receivers. Existing `apple_tv_host` and `apple_tv_id` options still work. The app migrates a single-TV installation automatically and backs up its previous records before using them.

Every tool accepts `tv`, for example `tv_look {"tv":"<room>"}`. Read `tv://tvs` for room names and stable IDs. If several TVs are paired, specify the room for changes or read their summaries with `tv_status {}`. Use `tv://screen/<id>` and `tv://apps/<id>` for room-specific resources.

`max_active_mirrors` defaults to one. If another room holds the slot, `tv_show` reports `mirror_busy` and identifies that room. Pass `replace: true` to stop its presentation. The holder list is in `data.holders`. Each receiver uses a separate stage origin. Each additional renderer uses a private loopback port. Apple TVs fetch bridge-generated MP4s from LAN port 8766. Uploaded bundles share one cache.

## Using the bridge

Configure Streamable HTTP at `${HA_URL}/api/atv_bridge/mcp`. Send the HA admin token in the `Authorization: Bearer <token>` header. Scripts can send tool arguments as a JSON body. This read-only check also works before choosing a room.

```sh
curl -fsS -H "Authorization: Bearer $(cat ~/.config/atv-bridge/ha_token)" \
  -H 'Content-Type: application/json' -d '{}' "$HA_URL/api/atv_bridge/tool/tv_status"
```

With several paired TVs, use `data.tvs[].status` for each receiver’s pairing and capability report. Add `"tv":"<room>"` to the examples below to target a receiver. Replace `<room>` with a name or ID from `tv://tvs`.

| Intent | Example |
| --- | --- |
| Check health and pairings | `tv_status {}` |
| Show a page and read it | `tv_show {"html":"<h1>Step 1</h1>"}`, then `tv_page_run {"script":"document.querySelector('h1').innerText"}` |
| Play media | `tv_play {"youtube":"jNQXAC9IVRw"}`, `tv_play {"media":"https://example.org/clip.mp4"}`, or `tv_play {"app":"HBO Max","title":"Succession"}` |
| Open an app | `tv_open {"app":"Settings","path":["Remotes and Devices"]}` |
| Read labels or inspect an image | `tv_look {}` or `tv_look {"image":true}` |
| Read what was watched | `tv_history {"since":"7d"}` or `tv_history {"app":"YouTube","limit":20}` |
| Move focus | `tv_navigate {"to":"Search","select":true}` |
| Send remote input | `tv_remote {"keys":["down","select"]}` or `tv_remote {"text":"Succession","submit":true}` |
| Batch remote work | `tv_sequence {"steps":[{"to":"General"},{"expect":{"focus":"General"}}]}` |
| Switch off | `tv_power {"on":false}` |

Control and presentation tools check power and wake the TV when needed. `screen.app.source` identifies accessibility observations and now-playing fallbacks. Playback metadata stays in `screen.playing` when another app has focus. Read `screen` and `verified` before choosing the next action. Text observations read the foreground app’s native accessibility tree without moving the inspector or scrolling the TV. Some apps expose few labels. During a menu transition, focus can briefly be null. Read again before sending another key. Request an image only on the intended TV. Capturing it can show a **Screenshot Taken** toast. The bridge sets `data.toast_visible` to `true` for a successful capture. `max_width` accepts 1–4096 pixels and defaults to 1280. DRM-protected video can appear black.

Use `tv://apps` for installed names and IDs, `tv://screen` for the latest text observation and `tv://page-guide` for page authoring. The `present` prompt accepts a topic and provides an HTML scaffold.

### Watch history

The bridge keeps a log of what each TV plays. Agents read it with `tv_history` to learn what you watch and for how long. Each entry has the app, the title, the series or channel when the app reports one, the start and end times, and the seconds actually played. `data.summary` adds totals and the top apps, series and artists. Filter with `since`, `until`, `app`, `search` and `tv`.

The TV pushes its now-playing state to the bridge, so recording adds no traffic and never wakes or changes a TV. The recorder samples when playback changes. Without a change it samples every **history_interval** seconds (default 300). After a change it waits **history_fast_interval** seconds (default 15) and doubles the wait until it reaches the interval. Repeated samples of one item extend one entry. An item that returns within four hours continues its entry. A rewatch is a new entry. A rewatch is an item that starts again in its first tenth after a break (another item, an idle TV, a lost session or a bridge restart), when its last position was at least half of its duration. Without a break, its last position must be in its last tenth. Paused time does not count as watched. Watched time can be low: when a sample reads a stop before the engine reports it, up to one **history_interval** of playing time is not counted.

The detail depends on the app. Hulu, Disney+, HBO Max, YouTube and Music report titles. Netflix reports no title, so its entries show only the app and the time. The bridge's own pages and clips are not recorded.

The history is private household data. It is stored only on the Home Assistant host, in the app's data at `/data/tvs/<id>/history.jsonl`, with file mode 0600. Home Assistant backups include it. Only callers with an HA administrator token can read it, through `tv_history` on the bridge MCP endpoint or the REST tool route. The Home Assistant LLM tools and Assist do not expose it. The bridge stores descriptive fields only. It never stores media links, profile identifiers or subscription identifiers. Each TV keeps up to **history_max_entries** entries (default 2,000). Set **watch_history** to `false` and restart the app to stop recording. The bridge then does not read or write the file, and `tv_history` returns no entries. Removing a TV from the bridge deletes its history.

### Presenting pages

Use `tv_page_run` to call functions on a page you authored. For other live pages, use DOM APIs such as `document.querySelector('#next').click()`. A later call follows the current document after navigation.

The **page_scripts** app option defaults to `true`. It lets callers with an HA admin token use `tv_page_run` on the private network. Scripts can read and change the page and make requests with its privileges. Set the option to `false` and restart the app to disable caller scripts. Use a new `tv_show` to refresh a recording.

Pass one of `url`, `html`, `files` or `bundle` to `tv_show`. Inline files need an `index.html` entry and relative asset paths. Prefix binary contents with `base64:`. For larger pages, POST a ZIP with `index.html` at its root to `/api/atv_bridge/bundle` and pass the returned `bundle`. The ZIP and its unpacked contents each have a 50 MiB limit. A ZIP may contain up to 8,192 entries. Stored pages share a 500 MiB cache.

For a live web app, keep its server running and pass its URL. Chromium on the Green loads the site, including its JavaScript, fetch requests and WebSockets. For a server on your own machine, share it with `tailscale serve`. Enable kernel networking in the HA Tailscale app. If HA cannot resolve MagicDNS names, record the existing upstream servers with `ha dns info` before running `ha dns options --servers dns://100.100.100.100`. That command replaces the Supervisor DNS server list. To use a LAN dashboard, grant access to its destination in the app options. The bridge checks tailnet DNS in `tv_status` even before a page is shown. The `tailnet_dns_missing` problem includes the HA DNS command. For each live URL, the bridge probes the server and checks the loaded Chromium document. A `page_unreachable` result includes the network error and a suggested fix. A site that refuses framing opens in the captured tab. The next show returns to the stage.

The **page_network_allow** option defaults to an empty list. To grant every presented page access to a trusted LAN service, add its exact DNS name, IP, CIDR or host and port, then restart the app. This example grants one service.

```yaml
page_network_allow:
  - "dashboard.example:8443"
```

A host without a port grants every TCP port on that host. Use brackets for an IPv6 host with a port, such as `[2001:db8::8]:8443`. Each grant applies to page scripts, embedded resources and the bridge’s live URL check. The HA host, the Supervisor subnet, loopback services and mDNS names stay protected. Use relative links and asset paths inside bundles. Public sites and reachable Tailscale Serve URLs work with the default options. The Apple TV fetches `tv_play` media URLs itself.

Expose an application function for the caller, for example `window.setStep = n => document.querySelector('h1').textContent = 'Step ' + n`. Invoke it with `tv_page_run {"script":"window.setStep(arg)","arg":2}`.

Use the 1920×1080 canvas and body text of at least 28 px. Audio plays by default. Set `audio` to `mute` for any page that plays no sound. It makes the mirror faster. On a Home Assistant Green, a mirror with audio uses about one more CPU core. A muted 1080p page renders about 40% more frames, and the encoder takes about 80 ms per frame instead of 120 ms. `keep_awake` defaults to true and requests a browser screen wake lock. Dismiss the page with `tv_show {}`. The `tv_hide` alias is accepted but omitted from `tools/list`. For a specific room use `tv_show {"tv":"<room>"}`.

### Remote input on a page

A person can use the Siri Remote on a mirrored page. When `tv_show` omits `remote`, it is `tvos` for `html`, `files` and `bundle` pages, and `focus` for `url` pages.

- `remote: "focus"` sends Left, Right, Up, Down and Select to the page as the keys `ArrowLeft`, `ArrowRight`, `ArrowUp`, `ArrowDown` and `Enter`, and adds a focus helper. Arrows move focus between links and controls, a focus ring shows the focused element, and Select activates it. The helper skips a key that the page handled with `preventDefault()`, so a page that handles its own keys keeps control.
- `remote: "tvos"` is `focus` with a tvOS look. The focused item lifts with a shadow and a white ring, Up and Down go to the next row on screen and take the item straight above or below, or else the nearest one, a row scrolls at once without a glide, and scrollbars are hidden. Use it for pages you write for the TV. Use `focus` for other pages, where a scaled item could be clipped or cover a neighbor.
- `remote: "off"` sends no presses.

For example, `tv_show {"html":"<h1>Pick one</h1>"}` uses `tvos`, and `tv_show {"url":"https://example.org/","remote":"off"}` sends no presses. The mode is set when the page shows, and a page that navigates keeps it.

Write pages for `tvos` with these layout rules:

- Use body text of 29 px or larger on the 1920×1080 canvas.
- Keep 60 px free at the top and bottom and 80 px at the sides.
- Put focusable items in rows and grids, so each arrow has an obvious target.
- Leave about 8% spare room around each focusable item. The focused item grows by 6%, and a parent with `overflow: hidden` would clip it.
- Put each row in a horizontal scroll container (`overflow-x: auto`). Mark a row that does not scroll with `data-tv-group`. At the end of a row, Left and Right stay in the row. They move to another row only when its item lies straight ahead. They can move between a row and an item outside every row, such as a menu beside the rows. When nothing else lies that way, they take the item that lies wholly that way and is nearest in height, however far up or down it is.
- Use a dark background.
- Do not rely on hover. Every control must be reachable with arrows and work with Enter.
- Do not run animation without input. The mirror shows about 10 frames per second, so constant motion looks rough.

The helper's timings and scales are in `window.__tvRemoteConfig`. They are preliminary and suit a mirror of about 10 frames per second. A page can set any of them in `window.__tvRemoteConfig` before the helper starts, and the helper keeps them over its defaults. A later change to a field applies at once.

The show result reports `data.remote` with the `mode` in effect, `state` and `reason`. It has no `data.remote` with `remote: "off"`. The page still shows when remote input cannot start. Later results report `screen.presenting.remote` with `state`, the number of `presses` and the `last` key with its time.

Remote input needs screen pairing. The observer reads presses from the TV's log on its existing connection while such a page is shown. It stops when the page is dismissed or replaced. If the connection to the TV drops, the bridge reports `state: "unavailable"` with a `reason` such as `stream_failed` or `tv_unreachable`, and retries until the state is `active` again. If the observer process restarts, the state is `starting` until the watch runs again. A recorded presentation (`data.transport` equal to `"video"`) cannot receive keys and reports `reason: "not_mirrored"`. The watch also stops when a mirror fails or falls back to a recording. [All reason values](../docs/mcp-api.md#remote-input)

What each remote action becomes:

| Remote | Action | Key in the page | Hardware status |
|---|---|---|---|
| Clickpad Siri Remote (2nd generation and later) | Click on the ring: left, right, up, down | `ArrowLeft`, `ArrowRight`, `ArrowUp`, `ArrowDown` | Verified on tvOS 27.2 |
| Clickpad Siri Remote | Click in the center | `Enter` | Verified on tvOS 27.2 |
| Clickpad Siri Remote | Swipe on the touch surface | Arrows, as on the touch remote | Seen on tvOS 27.2: a swipe moved one item, before momentum started at a lower speed. Its touch lines are not captured yet. |
| Touch-surface Siri Remote (1st generation) | Swipe | One arrow for each 0.4 of the pad that the finger travels | Recognizer checked against a real capture on tvOS 26.6.1. Not yet tested end to end. |
| Touch-surface Siri Remote | Brisk swipe or flick | Extra arrows after lift-off (momentum): 1 or 2 after a brisk swipe, 3 or 4 after a hard flick. A new touch stops them. | Speeds taken from the swipes of the capture. Not tested with a real flick |
| Touch-surface Siri Remote | Click anywhere on the surface | `Enter` | Verified on tvOS 26.6.1 |
| Touch-surface Siri Remote | Tap without a click | Nothing | By design, as in tvOS |
| `tv_remote` and `tv_sequence` keys | left, right, up, down, select | Arrows and `Enter` | Verified on tvOS 26.6.1 |
| Any remote | Menu, Home | Not forwarded. tvOS handles them and ends the mirror. | |
| Any remote | Play/Pause, volume | Not forwarded | |

A click during a touch sends only the key of the click, never an extra arrow. The swipe and flick tuning is preliminary.

To tune swipes, read the app log at the `info` level. For each finger contact that ends, the bridge logs one line that starts with `Remote touch:`. It has the positions as the TV logged them (`start`, `end`, `min`, `max`, `travel`), the number of `samples` and their `interval`, the `duration`, the `rest` between the last movement and the lift, the `span` (the coordinate range that counts as one pad), `peak_v` and `lift_v` in pad units per second, `steps`, `keys`, the number of `momentum` arrows, and `no_momentum`: why no momentum arrows went out (`clicked`, `too_short`, `rest_before_lift`, `no_dominant_axis`, `below_v_min`, `cancelled` or `no_lift`). The log keeps at most 20 of these lines per 10 seconds. When the watch starts, the log also shows the first touch line as the TV sent it, the first one that contains `down`, and touch lines with a shape that the bridge does not know.

Notes:

- Presses sent with `tv_remote` during the page are forwarded to the page too.
- Swipes are read from the TV's debug log. In a capture, the TV logged about 65 lines per second while a finger was on the remote, one burst of 513 lines when the remote woke, and less than one line per second otherwise.
- Set the app's log level to `debug` to log each forwarded key with its delay, and button lines that the bridge did not recognize. One press on the physical remote then shows what the TV logs for it. The key line splits the delay into `queue` (from the observer's read to the start of the send), `bind` (finding the content frame), `wake` (the script that must run in the frame before a key), `keys` (key-down and key-up) and the press log read after the key. A key that follows another one within half a second needs no wake. Keys that wait together, such as the arrows after a flick, go out together and share one line.

### Presentation mode

Set `presentation_mode` in the app options.

| Mode | Behavior |
| --- | --- |
| `mirror` (default) | Streams the live Chromium tab with page audio. |
| `play_url` | Records the stage as an H.264/AAC MP4 and plays it over AirPlay. Requires an image built with the repository’s pyatv fork. |
| `auto` | Tries mirroring twice. After two failed starts it uses MP4 until you hide the page. |

Mirroring defaults to a 1920×1080 canvas at 10 fps and 4000 kbps. Set `mirror_size` to `1280x720` or `1920x1080`, and `mirror_fps` to 1–60. The 720p setting uses 2500 kbps. A page shown with `audio: "mute"` uses the larger of `mirror_fps_muted` (default 15; `0` turns it off) and `mirror_fps`. On a Home Assistant Green at 1080p, a muted page sends about 9 fps at 15 and about 7 fps at 10. With audio, 15 sends no more frames than 10.

Upgrade note: `mirror_fps_muted` is a new option. After the update, an install with `mirror_fps` below 15 mirrors muted pages at 15 fps. Set `mirror_fps_muted` to `0` to keep the old behavior.

Live URLs always use mirroring. For recorded presentations, `tv_show` reports `data.transport` equal to `"video"` and may return `verified: false` while the clip is rendering. Static pages use a still frame for the duration of their audio, or 60 seconds without a finite audio track. Animated pages and page audio take time to record before playback begins. The clip loops while shown. If the TV closes the AirPlay session early, the bridge reopens it near the last playback position. Sending page data records a new clip.

For `tv_play {"media":"https://example.org/clip.mp4"}`, replace the example URL with an HTTP or HTTPS media URL the Apple TV can fetch. Normal EOF finishes playback. The bridge can make up to two further playback attempts, using the observed or estimated position.

## Interpreting results

- `uncertain` means delivery or playback could not be confirmed. Read the screen before another press.
- There is no purchase guard. If an agent needs approval for a paid action, it must get it before sending input.
- `not_paired` and `pairing_removed` call for control pairing. Use `tv_pair` or the HA pairing flow. For `access_blocked`, check the TV’s Allow Access settings.
- `tv_unreachable` means the wake or connection attempt failed. Wake the TV, check the network and check **apple_tv_host** if set.
- `screen_unavailable` means an accessibility read or screenshot failed. Check Remote Pairing.
- `busy` means another call held the TV for 10 seconds. Retry after it finishes.

Port 8765 accepts authenticated requests from loopback. Port 8766 serves the watchdog and page or media assets on the LAN. Treat hosted page and media content as visible to devices on that network.

## Pair from a phone or the Apple TV Bridge panel

Open the app's **Open Web UI** in Home Assistant. Choose the room and **Pair control** or **Pair screen**. Enter the displayed code with its leading zeros. The panel updates while pairing and lets you cancel a pending exchange.

To receive a phone prompt when a saved pairing becomes invalid, open the integration's **Configure → Pairing notifications** and select a Companion app phone. Reply to the notification with the digits shown on that TV. The phone receives the result of the exchange. An agent can start with `tv_pair {"tv":"<room>","kind":"screen","notify":true}`. For a control exchange, use `"kind":"control"`. Relay `data.ask_user`. If the state is `waiting_for_pairing_screen`, open **Remote App and Devices** on the TV and poll with the returned `pairing_id`. At `code_on_screen`, call again with the same TV, kind and pairing ID plus `code` as a string. Preserve leading zeros. Control takes four digits and screen takes six. Use `cancel: true` with the same TV, kind and pairing ID to cancel. For a control exchange with working screen pairing, `read_code_from_screen: true` can read an unambiguous AirPlay code from accessibility labels. [Companion app actionable notifications](https://companion.home-assistant.io/docs/notifications/actionable-notifications/)

Set `default_profile` to the preferred tvOS profile and `app_profile` to an in-app profile name. With neither set, the bridge uses the focused tvOS profile and prefers that name in apps. It selects a Kids profile only when it is the sole profile. The bridge chooses accessible privacy-preserving first-run actions and reports those choices in tool evidence.
