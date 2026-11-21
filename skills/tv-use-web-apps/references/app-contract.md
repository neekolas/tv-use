# TV app contract

Rules every TV web app in this skill follows. The reference apps in `assets/apps/` are built to it. Follow it for your own apps, so any agent can drive them the same way.

## One file, no network

- Ship one `index.html` with inline CSS, JavaScript and SVG. Keep it under about 60 KB.
- Load nothing from the network. Ship photos as small JPEGs beside `index.html` in a bundle.
- Fonts come from `fonts/` beside the file: `inter`, `fraunces`, `newsreader`, `bricolage` and `instrument-serif` (`.woff2`, latin, variable where available; see `assets/apps/fonts/`). Declare them with `@font-face { src: url(fonts/inter.woff2) }`. `upload-bundle.sh` includes the folder.
- End every font stack with `"Noto Sans"` or `"Noto Serif"`, then `system-ui`. The HA host has only Noto, and a page sent inline has no `fonts/`. Check that the design still holds with `preview.py --no-fonts`.
- The page runs in a sandbox with an opaque origin. `localStorage`, cookies and IndexedDB fail. Keep state in memory.
- Keep the file small: every rule, function and comment earns its place. Generate repeated markup in JS rather than copying it.

## Canvas

- Lay out a fixed 1920×1080 stage and scale it to the window, so the same layout works at 1280×720:

  ```css
  html, body { margin: 0; height: 100%; overflow: hidden; background: var(--bg); }
  #stage { position: absolute; left: 0; top: 0; width: 1920px; height: 1080px; transform-origin: 0 0; }
  ```

  ```js
  const fit = () => { const s = Math.min(innerWidth / 1920, innerHeight / 1080);
    stage.style.transform = `translate(${(innerWidth - 1920 * s) / 2}px, ${(innerHeight - 1080 * s) / 2}px) scale(${s})`; };
  new ResizeObserver(fit).observe(document.documentElement);
  ```

- Keep content inside a safe area: 80 px from the sides and 60 px from the top and bottom.
- Fit each view on one screen. Never scroll. Page through long lists instead.

## Type and color

- Body text at least 32 px. Secondary text at least 28 px. Headings 56 px or more. The key number or word on a view can be 120 px or more.
- Below 56 px, use weights 500 to 800. Thin weights break up in the mirror's video. Display type at 72 px or more can go to 400.
- No fine grain, noise textures or 1 px hairlines: video encoding smears them. Use smooth, low-contrast gradients and rules of 2 px or more.
- Use `font-variant-numeric: tabular-nums` for clocks, timers and scores.
- Use a dark background, not pure black: about `#0d0f14`. Use text near `#f3f4f7`, and secondary text at 7:1 contrast or better against the background.
- Pick one accent color per app. Use it for focus, the current item and the status dot. Do not rely on color alone; pair it with a number, a label or a shape.
- At most about 40 words of body text per view. People read from several metres away.

## Motion

- The TV receives a mirror at about 10 frames per second, and the HA host renders and encodes it. Fast motion stutters and costs CPU.
- Animate only on change: one transition of 300 ms or less, using `opacity` or `transform`.
- No continuous animation, with two exceptions: the status dot, which changes once a second, and clocks or timers that tick once a second.
- No `backdrop-filter`, large animated shadows, canvas or WebGL loops, or video inside the page.

## Talking to the page

The agent changes the page only through `tv_page_run`. Expose exactly two globals.

### `window.app`

- `app.apply(patch)` is the single entry point for every change: loading data, moving between views, selecting an item, starting a timer. It takes a plain JSON object and returns `app.state()`.
- Patches are partial. Keys you leave out keep their value. Applying the same patch twice has the same result as once.
- Unknown keys do not throw. Report them in the returned state as `ignored: ["key"]`.
- Invalid values throw an `Error` with a message that says what to send instead. The agent sees it as `script_error`.
- `app.state()` returns a small JSON object: the current view, the ids or numbers on screen, and anything the agent needs for the next call. Keep it under about 1 KB.
- `app.apply({demo: true})` loads the built-in sample data. Opening the file with `?demo` in the URL does the same. Without either, the app starts in its pending state.

### `window.tv`

Every app includes the same status kit as `assets/pending-page.html`:

| Call | Effect |
|---|---|
| `tv.working(text)` | Shows a status line with a dot that changes once a second. |
| `tv.pending(name, label)` | Marks the element with `data-slot="name"` as pending, with a label. |
| `tv.fill(name, html)` | Fills that element and clears its pending look. |
| `tv.done()` | Hides the status line. |

Each returns `{working, pending}`.

## Voice first

- The user talks, and the agent drives the page. There is no pointer and usually no remote.
- Number every selectable item from 1 on screen, so "the third one" maps to an id. Return the numbers in `app.state()`.
- Do not put instructions on the screen, such as "Say next to continue". If the next action is not obvious, a short hint in the footer is acceptable.
- Show the current item or selection with the accent color and a visible outline or marker, not color alone.
- When a view is waiting for data, show its layout with pending places, not an empty screen or a spinner.

## Remote input

Every app works with the Siri Remote as well as by voice. `tv_show` shows a page you write with `remote: "tvos"` by default (see [remote.md](remote.md)).

- Make every action a person can take a real `<button>`, or an element with `tabindex="0"`, that `click` handles. Enter activates it. Nothing is hover-only.
- Lay focusable items out in rows and grids. Wrap each row in an element with `data-tv-group`. Leave about 8% spare room around each one, because the focused item grows 6%; no `overflow: hidden` on its parent.
- Menu (Back) on the remote ends the page for good: the bridge dismisses it and does not show it again. Put an on-screen Back button wherever a view has a way back.
- Route each remote action through the same `PATCH` handler a voice patch uses, so voice and remote stay in step. Then call `tv.input(action, detail)`, such as `tv.input("pick", {item: 3})`, so the agent sees what the person did.
- After each render, keep focus on a sensible item: the current step's Next button, the focused card, the first answer.
- Pages mostly for reading or glancing (an answer, a dashboard) show no focus ring until the first remote press. Pages built for choosing may focus their first control at once: give it `autofocus`. It shows focus when the page appears, and the first arrow moves from it.
- List the remote actions in the header: which control does what, and what `tv.input` reports.

## Code layout

Every app uses the same layout, in this order, so you can find and extend any part without reading the rest.

1. **Header comment.** What the app is for, every patch key with an example, and the likely voice turns mapped to patches.
2. **`<style>`.** Tokens first (`:root` colors, fonts, sizes), then the layout, then one block per view.
3. **Markup.** `#stage` with one `<section data-view="name">` per view.
4. **Fit script and tv kit,** unchanged.
5. **App script:**

   ```js
   const SAMPLE = {...};          // demo data, also the data shape
   const state = {...};           // everything on screen comes from this
   const VIEWS = { name() {...} };  // one render function per view, reads state only
   const PATCH = { key(value) {...} };  // one handler per patch key, changes state only
   function apply(patch) { /* runs PATCH handlers in order, then renders */ }
   window.app = { apply, state: () => ({...}) };
   ```

To add a patch key, add one `PATCH` entry and one header line. To add a view, add one `<section>`, one `VIEWS` entry and its CSS block. Name handlers after their keys. No frameworks and no clever indirection. Comment only where someone will extend the app.

## Markup

- Use semantic elements: one `h1` per view, lists for lists, `role="status"` on the status line.
- Give every element that the agent may read a stable `id` or `data-slot`.
- Escape text that comes from data. Use `textContent`, or an escape helper before `innerHTML`.
- Keep the DOM small, under about 500 elements.

## Deliver it

- With a shell: upload the file once as a bundle with `scripts/upload-bundle.sh`. Then show it with `tv_show {"bundle":"<id>"}` and change it only with `app.apply`. Reuse the bundle ID until the bridge reports it missing.
- Without a shell: pass the whole file as `tv_show {"html": "..."}`.
- Allow 0.25 seconds between `tv_page_run` calls, or combine changes into one patch.
