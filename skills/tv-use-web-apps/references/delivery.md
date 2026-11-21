# Put your content on the TV

Use the same `tv` selector for showing, updating and hiding a page. Examples omit it when a default receiver is available.

## Pick a source

To present a page, pass exactly one source. You can also set `title` (reported in `screen.presenting`) and `audio: "mute"` (default `"play"`).

| Source | Use when | Notes |
|---|---|---|
| `html` | One self-contained page | Inline the CSS, scripts and small images. |
| `files` | A page with a few assets | A map of relative path to content. It must include `index.html`. Prefix binary content with `base64:`. |
| `bundle` | Many or large assets | Upload a ZIP first, then pass its ID. |
| `url` | A live site, or a server you run | The HA host loads it. Your server must stay up. |

A ZIP must be at most 50 MiB compressed and 50 MiB unpacked, with at most 8192 entries. The bridge converts inline `files` to a ZIP under the same limits. JSON and base64 add request overhead. Use a raw ZIP for large assets. Stored pages share a 500 MiB cache on the HA host.

## Upload a bundle

Put `index.html` and its assets in a directory named `site`. From its parent directory, create a new archive and send it as the request body. Set `HA_URL` and `HA_TOKEN` as in the **tv-use-connect** skill. `scripts/upload-bundle.sh` does these steps for you.

```sh
(cd site && zip -r ../site.zip .)
```

```sh
curl --fail-with-body -sS -X POST -H "Authorization: Bearer $HA_TOKEN" -H "Content-Type: application/zip" \
  --data-binary @site.zip "$HA_URL/api/atv_bridge/bundle"
# => {"bundle": "<32 hex characters>"}
```

Then call `tv_show {"bundle": "<id>"}`. An invalid ZIP within the request size limit returns HTTP 200 with `"ok": false`, `error.code` `invalid_bundle` and the fix in `error.remediation`. A body over 50 MiB returns HTTP 413. All TVs on the bridge share uploaded bundles.

## Live URLs

- Chromium on the HA host loads live HTTP and HTTPS URLs. Scripts, fetch, WebSockets and live reload work. Use a public site or a reachable tailnet URL. LAN dashboards require an operator grant in `page_network_allow`.
- If the content does not need your server, send `html`, `files` or a `bundle` instead. They need no network access.
- On the home network, a server on your own machine needs an operator grant. Ask the user to add your machine's LAN IP address and port to `page_network_allow`, then pass `http://<LAN IP>:<port>/` to `tv_show`. Use the IP address, not a `.local` name. The server must listen on the LAN interface, such as `0.0.0.0`, not only on `localhost`.
- Outside the home, or without a grant, share the server on the tailnet:

  ```sh
  tailscale serve --bg --https=8443 http://127.0.0.1:5173
  ```

  Then call `tv_show {"url": "https://<agent-machine>.<tailnet>.ts.net:8443/"}`. Stop sharing with `tailscale serve --https=8443 off`.
- If your dev server rejects unknown host names (for example Vite's `server.allowedHosts`), allow the `ts.net` name.
- The bridge probes the URL and checks the loaded document. On `page_unreachable`, read `error.fix`. It can point to DNS, TLS, a refused connection, a timeout or a server error.
- If the probe finds a framing restriction such as `X-Frame-Options: DENY` or the stage times out, the bridge tries top-level navigation. Use `tv_page_run` in either presentation mode.

## How long a page stays

- A presentation continues after the agent disconnects. Hiding it, showing another page, starting playback, opening an app or changing power can end it. Another room can take its mirror slot with `replace: true`. Restarting the bridge also ends it.
- The stage crossfades between embedded pages. Top-level navigation and transport changes can restart or reload the browser. For small changes, call the page’s functions with `tv_page_run`.
- `html`, `files` and `bundle` pages are stored on the HA host, so they outlive your session. When the cache is full, the bridge deletes the oldest pages that are not on screen. If a stored bundle is gone, `tv_show` returns `page_unreachable` with "The bundle is unavailable"; upload it again.
- A `url` page needs your server for as long as it is on screen. If the content must outlive your session, send `html` or `files` instead.
- When you finish, call `tv_show {}`. Stop your server only after that.

## Operate a mirrored page

1. **Your own page.** Expose small functions on `window`. Call them with `tv_page_run {"script":"window.setStep(arg)","arg":2}`. Return the resulting application state or read it in a later script.
2. **Any other page.** Query the element you need. For discovery, narrow `document.querySelectorAll()` to likely controls or a known container. Use DOM methods to read, fill, click or submit. The next call runs in the current document. [DOM recipes](recipes.md#read-and-operate-a-page)

The capture browser allows autoplay with sound. Choose `audio: "play"` to send page audio. Synthetic events do not grant user activation for gesture-gated APIs. Check the current document before retrying a timed-out script. A recording can be refreshed with a new `tv_show`.

## Sandbox and privacy

Page control uses the HA administrator token on the private network. Caller JavaScript runs with the privileges of the presented document, including access to its field values and network requests. Set `page_scripts: false` in the app options to disable `tv_page_run`.

- Use relative asset paths, links such as `details.html`, and anchors such as `#step` in bundles. Page JavaScript, including `tv_page_run`, can reach public sites, tailnet peers, active stage origins and configured destinations. Requests to HA, its API and debug ports, mDNS names and other private addresses fail by default.
- `html`, `files` and `bundle` pages run in a sandbox with an opaque origin. Page scripts cannot use origin-based storage such as `localStorage`. Keep state in your own process or in the current document.
- Load assets with relative paths.
- The HA host serves stored pages on LAN port 8766 under `/c/<page_id>/<file>` without HA authentication. Do not put secrets or private data you would not show on the LAN into a page.

## Mirror slots

The bridge mirrors a limited number of pages at once across all TVs. The default is one (app option `max_active_mirrors`). If another TV holds the slot, `tv_show` returns `mirror_busy` with `data.holders`. `tv_show` with `replace: true` stops that TV's page and takes the slot. Someone may be watching the other TV, so ask the user first. `tv_show {"tv": "<room>"}` ends the page on that TV only.
