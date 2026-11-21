# Let someone use the Siri Remote on a page

Use this when a person at the TV should move through or pick things on a page you show. `tv_show` turns the Siri Remote on by default. Set `remote` only to change the mode:

| `remote` | Default for | Effect |
|---|---|---|
| `"tvos"` | `html`, `files` and `bundle`: pages you write, including every reference app | tvOS-style focus: the focused tile lifts with a ring, arrows move straight up, down, left or right by geometry, side menus are reachable and scrollbars are hidden. Enter activates. Follow the layout rules in [app-contract.md](app-contract.md#remote-input). |
| `"focus"` | `url`: pages you didn't write | A plain focus ring. Arrows move to the nearest link or control; Enter activates it. |
| `"off"` | | Presses aren't forwarded. |

Both modes send trusted `ArrowLeft`, `ArrowRight`, `ArrowUp`, `ArrowDown` and `Enter` events. A page that handles a key and calls `preventDefault()` keeps control of it.

Also pass `audio: "mute"` unless the page plays sound. On a Home Assistant Green that saves about one CPU core and sends about 9 frames per second instead of 7.

Example: `tv_show {"bundle":"<pick-one ID>","audio":"mute"}` uses `tvos`. `tv_show {"url":"https://example.org/","remote":"off"}` sends no presses.

## Check it

- `data.remote.mode` is the mode in effect. `data.remote.state` is `"active"` when presses arrive. With `"off"`, there is no `data.remote`.
- `"unavailable"` comes with a `reason`: `screen_not_paired` → `tv_pair {"kind":"screen"}`; `not_mirrored` → the page is a recording and gets no input. The page shows either way.
- Presses are counted in `screen.presenting.remote` and `window.__tvRemote`.
- What the person did in a reference app: `tv_page_run {"script":"tv.state().input"}` gives `count`, `last` (`{n, action, detail, at}`) and the five most recent actions. Each app's header lists the actions it reports.

## Remotes and limits

- Clickpad Siri Remote: ring clicks become arrows and the center becomes Enter. Verified on tvOS 27.2.
- 1st-gen touch remote: swipes become arrows (a fast flick moves several items) and any click becomes Enter. Clicks are verified; swipes only by replaying a real capture.
- Menu and Home stay with tvOS, and Menu ends the mirror. Play/Pause and volume aren't forwarded.
- Your own `tv_remote` presses reach the page too.
- Press to page takes about 150–250 ms.
