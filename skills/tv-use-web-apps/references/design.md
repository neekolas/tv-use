# Design for the TV

How the bridge renders and mirrors a page, and what that means for type, motion, input and audio. The [app contract](app-contract.md) turns these rules into a checklist.

## Canvas and type

- Design for the configured output. Mirroring defaults to 1920×1080 and also supports 1280×720. Read `data.size` after `tv_show`. Lay out a fixed 1920×1080 stage and scale it to the window, as in the [app contract](app-contract.md#canvas). Fit each step on one screen.
- Use body text of at least 32 px, secondary text of at least 28 px and headings of 56 px or more. People read from several metres away.
- Use high contrast and few words per screen. Dark backgrounds suit a living room in the evening.
- Keep a margin of about 60 px at the top and bottom and 80 px at the sides, on a 1080p layout.
- If you need to inspect the layout, `tv_look {"image":true,"tv":"<room>"}` takes a screenshot and shows a toast.

## Motion

- The TV receives the page as a mirror at about 10 frames per second by default. Fast motion can look uneven at that rate.
- Prefer static layouts. Change content in steps, on a page function call or on a data refresh. Change only what changed, for example the numbers on a dashboard.
- Avoid continuous animation, video inside the page, canvas or WebGL loops, and heavy blur or shadow effects. Rendering and encoding share the HA host’s CPU.
- A short fade between steps is fine.
- To play a video, use `tv_play {"media": "<URL>"}` or an app, not a page. The Apple TV fetches `media` URLs itself, so the URL must be reachable from the TV.

## Input

- Siri Remote keys operate tvOS. For agent input on a mirrored page, give each control an accessible name and use a status region for visible changes. Give controls stable IDs so agents can call `document.querySelector('#next').click()` or read a status directly in `tv_page_run`.
- Refresh recorded pages with a new `tv_show`.

## Audio and reading aloud

- Page audio (`<audio>`, Web Audio) plays on the TV. The bridge browser allows autoplay, so a page can start audio without a click.
- Pass `audio: "mute"` to `tv_show` for a silent page. The mirror then uses about one CPU core less on a Home Assistant Green and sends about 9 fps instead of 7 at 1080p.
- To read text aloud, generate the speech audio yourself with any text-to-speech you have. Ship the clips in `files` or a bundle, or serve them from your live server. Play a clip from the page, for example when the caller invokes your playback function.
- Keep one clip per step or paragraph, so your playback function can jump to any of them.

## Recorded mode

For stored content in `play_url` mode or after `auto` falls back, `tv_show` returns `data.transport: "video"`. The bridge records the page to an MP4 and plays it over AirPlay. The first result may say `verified: false` while it renders, and a new `tv_show` records a new clip. Keep pages static in this mode. Live URLs always use mirroring.
