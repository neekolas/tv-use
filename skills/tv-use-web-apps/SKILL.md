---
name: tv-use-web-apps
description: Build and show web apps on an Apple TV through tv-use. Covers the TV app contract, design for a mirrored 10-foot screen, reference apps (cook-along, pick-one, home board, quiz, answer card), bundle upload and live updates with tv_page_run. Use to put a page, dashboard, game or visual answer on the TV.
---

# Build web apps for the TV

`tv_show` puts a page on the TV: Chromium on the HA host renders it in a sandboxed iframe and mirrors it at about 10 fps. `tv_page_run` runs JavaScript in that page, so you change it in place.

## Start from a reference app

Each file in `assets/apps/` is one self-contained page. Its header comment lists its patch API and typical voice turns. Adapt one before writing your own.

| App | For |
|---|---|
| `cook-along.html` | Recipes and other step-by-step tasks: steps, timers, checklist |
| `pick-one.html` | Choosing among options: numbered cards, details, filter, pick |
| `home-board.html` | An ambient dashboard that updates quietly for hours |
| `quiz-night.html` | A trivia game: questions, countdown, reveal, scores |
| `answer-card.html` | A visual answer that fills in as results arrive |
| `assets/pending-page.html` | Anything else: a title, pending places, a status line |

All of them follow [app-contract.md](references/app-contract.md): `app.apply(patch)` changes the page and returns `app.state()`; `tv.working(text)`, `tv.pending(name, label)`, `tv.fill(name, html)` and `tv.done()` show progress.

## Show and update

1. Upload once: `scripts/upload-bundle.sh assets/apps/<app>.html` prints a bundle ID (needs `HA_URL`, `HA_TOKEN`). Without a shell, send the file as `tv_show {"html":"..."}`.
2. Show it before the data is ready: `tv_show {"tv":"<room>","bundle":"<id>","audio":"mute"}`. It starts in its pending layout, and the Siri Remote works on it in `tvos` mode. Leave out `audio` only for pages that play sound: mute saves about one CPU core on a Home Assistant Green and sends more frames.
3. Patch it: `tv_page_run {"tv":"<room>","script":"app.apply(arg); return tv.working('…')","arg":{...}}`. One script per change; the bridge refuses a second within 0.25 s.
4. `tv.done()` when finished. `tv_show {"tv":"<room>"}` hides it.

Reuse the bundle ID until `tv_show` reports it missing.

## Preview without a TV

```sh
uv run --with playwright scripts/preview.py assets/apps/pick-one.html /tmp/out \
  --step '{"demo":true}' --step '{"view":"detail","item":2}'
```

It renders in the bridge's sandbox with an installed Chrome and saves a PNG per step. Check `--size 1280x720` too.

## References

| File | Load to |
|---|---|
| [app-contract.md](references/app-contract.md) | Build or change an app |
| [design.md](references/design.md) | Motion at 10 fps, input, audio, recorded mode |
| [delivery.md](references/delivery.md) | Choose `html`, `files`, `bundle` or a live `url`; limits, lifetime, sandbox, mirror slots |
| [remote.md](references/remote.md) | Let someone at the TV use the Siri Remote on a page |
| [recipes.md](references/recipes.md) | DOM recipes for any page, a live dashboard, a slideshow, reading aloud |
