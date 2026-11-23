# tv-use

**Make an Apple TV a first-class device for AI agents.** tv-use lets an agent see what is on the TV, show anything it can draw, open any app at the right screen and drive the remote. It runs on Home Assistant and gives agents one MCP endpoint. It is built to be driven by an agent in a real-time voice conversation.

> **Works with any agent.** Claude, Codex, or remote agents like Grok Bot and Muse can all drive your TV. They can reach it directly over your local network, or remotely via Home Assistant and Tailscale.

![A pork bulgogi recipe shown full screen on a TV, with the prompt "Say next when you're ready"](docs/images/recipe-title.jpg)

## Imagine if Siri wasn't useless

Apple TV is the best TV hardware you can buy. It's fast and reliable. It isn't creepily spying on you [[1]](https://www.techradar.com/televisions/your-tv-is-a-mass-surveillance-system-says-texas-and-the-state-is-suing-lg-samsung-hisense-tcl-and-more-to-stop-it) [[2]](https://www.ucdavis.edu/news/your-smart-tv-watching-what-you-watch). It is also completely impenetrable for AI agents.

Rather than wait for Siri to grow a brain, people are trying to drive their Apple TV through Home Assistant. The only problem is that it barely works. Agents can push buttons on the remote, but they can't see what is happening on screen or show content they made. Even something as simple as putting a recipe up on your TV is basically impossible today.

Home Assistant's Apple TV integration is based on [pyatv](https://github.com/postlund/pyatv), which is barely maintained and doesn't properly support tvOS 27. Its last release came out in June 2026, before tvOS 27. The fixes for AirPlay PIN pairing on tvOS 27 ([#2894](https://github.com/postlund/pyatv/pull/2894)) and for now-playing state on tvOS 27.2 ([#2953](https://github.com/postlund/pyatv/pull/2953)) are still open pull requests, along with dozens of others. And even if it did support tvOS 27, it doesn't offer the full set of APIs that Apple already exposes to interact with the screen.

We need something like the [Chrome DevTools Protocol](https://chromedevtools.github.io/devtools-protocol/) for Apple TV: a way to drive it remotely and see the result. That's what tv-use is.

Here is what it adds over the built-in integration:

| An agent needs to | Built-in integration | tv-use |
| --- | --- | --- |
| Know what is on screen | Current app and now-playing data only | `tv_look` reads the screen's accessibility labels and which item has focus |
| See screens without good labels | No screenshots | `tv_look {"image":true}` returns a screenshot |
| Show its own content | Media URLs only | `tv_show` puts any web page, document, slide deck, photo set or live dashboard on the TV |
| Open a show in the right app | A few deep-link examples and "try a share link" | A tested deep-link catalog, and a check of where the link landed |

Deep links are the clearest example. How would an agent know that an HBO Max episode opens with `https://play.hbomax.com/video/watch/{id}`? Or that a Netflix title link opens Netflix Home, not the title? tv-use keeps a catalog of links for 47 services, with trial results from a real Apple TV, and gives it to the agent. After each launch, it reads the screen to report where the TV actually landed, and it can answer the profile picker. [Deep links by service](skills/tv-use-connect/references/deeplinks.md)

## What tv-use does

tv-use turns an Apple TV into hardware an agent can fully script. It installs on Home Assistant as an app and a custom integration. It uses current Apple protocols:

- **RemotePairing** to read the screen's accessibility text and take screenshots.
- **AirPlay mirroring** to show pages rendered by Chromium on the Home Assistant host.
- **Companion** for the remote, keyboard, app launches and power, through a [pyatv fork](docs/pyatv-fork.md) with 20 patches for tvOS 27.

Every result tells the agent what changed on screen, so it can check its work instead of guessing. Calls are fast enough for a voice conversation. A burst of remote keys returns the new screen in about 2 seconds.

![tvOS Settings with Notifications focused](docs/live-accessibility/settings-before.png)

On this screen, `tv_look` returns each menu label, says that **Notifications** has focus, and names the app. The agent can then call `tv_navigate {"to":"Remotes and Devices","select":true}` without counting key presses.

## What you can put on the TV

The TV becomes a shared screen for whatever you and the agent are working on. Some things to try:

- **Cook from a recipe.** Ask for a recipe scaled to what is in your fridge. The agent makes the slides and puts them on the TV. Say "next" to move to the next step.

  ![The ingredients slide of the same recipe, with amounts scaled to a 1.225 kg roast](docs/images/recipe-ingredients.jpg)

- **Watch a show.** "Put on The Last of Us." The agent finds which installed service has it, opens it with a deep link, picks your profile and checks that it is playing.
- **Look at your photos.** Give the agent a folder of photos. It builds a slideshow, and you can say "go back to the first one" or "pause".
- **Watch a live dashboard.** Serve a dashboard from your laptop over Tailscale. The TV shows it and it keeps updating.
- **Listen to an article.** The agent shows the article in large type and reads it aloud, one paragraph at a time.
- **Find a podcast.** The agent searches YouTube for the episode and plays it.
- **Fix a setting.** "Turn on subtitles." The agent opens Settings, reads each menu and selects items by label.

[Page recipes](skills/tv-use-web-apps/references/recipes.md) · [Web apps skill](skills/tv-use-web-apps/SKILL.md)

## Install and connect

In Home Assistant, tv-use appears as the app and integration **Apple TV Agent Bridge**.

tv-use targets **Apple TV 4K on tvOS 27 or later**, the recommended and tested baseline. tvOS 26.6 also works but is less tested: pairing, control and screen reading all worked on an Apple TV 4K (1st generation), which cannot run tvOS 27. Testing included an Apple TV 4K (2nd generation) on tvOS 27.2 (24K122) and a Home Assistant Green. Discovery works without a static IP. No Apple Developer account is required. Nothing needs to be installed on the TV.

1. Put Home Assistant and the TVs on the same LAN. Install the local app **Apple TV Agent Bridge** and copy the custom integration into Home Assistant. The setup guide covers both a manual install and a workstation deploy. [Setup guide](docs/SETUP.md) · [Workstation deploy](docs/deploy.md)
2. Restart Home Assistant Core. If the TV already has a built-in Apple TV integration entry, disable that entry before pairing. Accept the bridge discovery in **Settings → Devices & services**. Choose the TV if prompted. Enter its AirPlay PIN. For screen reading, open **Settings → Remotes and Devices → Remote App and Devices** on the TV before requesting the screen PIN. Use **Add Apple TV** on the same integration entry for another TV.
3. Create a long-lived access token in a Home Assistant administrator’s profile. Give your MCP client the Streamable HTTP endpoint at `/api/atv_bridge/mcp` on your Home Assistant URL and the header `Authorization: Bearer <token>`. If the agent runs on your home network, use the local origin, such as `http://homeassistant.local:8123`. If it runs outside the home, use HA’s tailnet hostname. The same token works for both. For Claude Code, set `HA_TOKEN` to the token in your shell and use your own HA origin in this example. [Client setup](docs/SETUP.md#connect-an-agent)

   ```sh
   HA_URL='http://homeassistant.local:8123'
   claude mcp add --transport http apple-tv "$HA_URL/api/atv_bridge/mcp" \
     --header "Authorization: Bearer $HA_TOKEN"
   ```

4. Call `tv_status {}`. With one paired TV, check `data.tv.reachable`, `data.pairings` and `data.problems`. With several paired TVs, check each row in `data.tvs`, or call `tv_status {"tv":"<TV name>"}` for one TV.

### Fastest setup: an agent in the house

Run Claude Code, Codex or Grok Bot on a machine on the same network as Home Assistant, such as a desktop or a home server. It reaches HA directly at `homeassistant.local`, with no Tailscale. Each call skips the trip between a remote agent and the home. From a Mac on the home network, `tv_status` returned in about 0.07 seconds and a text-only `tv_look` in about 0.6 seconds. Use Tailscale only for agents that run outside the house. [Choose local or remote](docs/SETUP.md#choose-local-or-remote)

### Alternative: run without Home Assistant

The same app image also runs as a plain MCP server in a Docker container, for example with OrbStack on a Mac. It connects to the TVs directly and needs no Home Assistant. A Mac renders and mirrors pages much faster than a Home Assistant Green, but it must stay on and awake. You configure it with environment variables or command flags and pair with `tv_pair`. [Run without Home Assistant](docs/standalone.md)

## Add the skills to your agent

Four [agent skills](skills/) teach an agent to use tv-use: **tv-use-setup** installs and pairs the bridge, **tv-use-connect** reaches and controls the TV, **tv-use-web-apps** builds pages for it, with five reference apps, and **tv-use-realtime** runs live voice sessions. The repository is a plugin for each agent below.

| Agent | Install | MCP server |
| --- | --- | --- |
| Claude Code | `/plugin install tv-use --marketplace neekolas/tv-use` | Included. It asks for your HA URL and token. |
| claude.ai, Claude Desktop, Cowork | **Customize → Plugins → Add → Add marketplace**, then `neekolas/tv-use` | Add a custom connector by hand. It needs an HTTPS URL that Anthropic can reach. |
| Codex, ChatGPT desktop | `codex plugin marketplace add neekolas/tv-use`, then install it from `/plugins` | `codex mcp add tv-use --url "$HA_URL/api/atv_bridge/mcp" --bearer-token-env-var HA_TOKEN` |
| GitHub Copilot CLI | `copilot plugin install neekolas/tv-use` | Add it by hand, as in [connect.md](skills/tv-use-connect/references/connect.md). |
| VS Code | **Chat: Install Plugin from Source**, then `https://github.com/neekolas/tv-use` | Add it by hand. |
| Google Antigravity | `agy plugin install https://github.com/neekolas/tv-use`, then `agy plugin enable tv-use` | Included. Enabling asks for your HA URL and token. |
| Gemini CLI | `gemini extensions install https://github.com/neekolas/tv-use` | Add it by hand. |
| Muse Code | `muse plugins install . --scope user` in a clone, then `muse plugins approve tv-use` | Add a `streamable_http` entry to `~/.config/muse/settings.json`. |
| OpenCode and others | `npx skills add neekolas/tv-use` | Add a remote server with `"oauth": false` and the bearer header. |

Each manifest names only the skills, except where noted. Each household has its own HA URL and token, so most formats cannot ship the server. The Muse Code manifest follows a working third-party plugin, because Meta does not document the format.

**Privacy.** The skills and the MCP server talk only to your bridge, on Home Assistant or a standalone host. Pages you show load from that host. Nothing is sent to the plugin author.

## Operate a TV

Every tool accepts an optional `tv`. Use the name or stable bridge ID from `tv://tvs`. One paired TV is the default, even if others await pairing. With several paired TVs, name the TV for actions. An untargeted `tv_status` or `tv_look` returns summaries. Use an ID when names overlap.

| Task | MCP call | What to expect |
| --- | --- | --- |
| Show a page | `tv_show {"tv":"<TV name>","html":"<h1>Dinner is ready</h1>"}` | Chromium renders on the HA host. tvpipe mirrors the tab with audio. A live HTTP(S) URL, inline files or an uploaded ZIP can supply the page instead. |
| Change the page | `tv_page_run {"tv":"<TV name>","script":"window.setStep(arg)","arg":3}` | Call a function on your live page. Dismiss it with `tv_show {"tv":"<TV name>"}`. |
| Start playback | `tv_play {"tv":"<TV name>","youtube":"jNQXAC9IVRw"}` | Accepts a YouTube ID or URL, media URL, deep link, or an app and title. Playback and title search depend on the installed app. |
| Open an app | `tv_open {"tv":"<TV name>","app":"Settings"}` | Optionally supply a search or a menu path. Read `tv://apps/<TV ID>` for installed apps. |
| Read the screen | `tv_look {"tv":"<TV name>"}` | Reads accessibility labels and focus. Some apps expose little text. Set `image` to `true` for a screenshot. tvOS can show a screenshot notice. |
| Navigate by label | `tv_navigate {"tv":"<TV name>","to":"General","select":true}` | Targets a visible label. Requires screen pairing. |
| Use the remote | `tv_remote {"tv":"<TV name>","keys":["down"]}` | Sends keys, a swipe or text. Text entry requires a focused keyboard field. |
| Batch remote work | `tv_sequence {"tv":"<TV name>","steps":[{"to":"General"},{"expect":{"focus":"General"}}]}` | Runs on one receiver and stops at the first failed step. |
| Set power | `tv_power {"tv":"<TV name>","on":false}` | Checks the resulting Apple TV power state. |
| Repair pairing | `tv_pair {"tv":"<TV name>","kind":"screen"}` | Starts a PIN exchange for a registered TV. Follow `data.ask_user` and use the returned pairing ID. Use `kind:"control"` for AirPlay. |
| Check health | `tv_status {"tv":"<TV name>"}` | Reports reachability, pairing state, capabilities and problems with suggested fixes. |

Tool results include `ok`, `summary`, `verified`, `evidence` and `data`. An acknowledged action can have `verified: false`. Read the evidence and check the screen before sending another key. Failures include `error.code`, `error.message`, `error.retryable` and `error.fix`. Most tools return a compact `screen`. Pairing uses unknown screen values, and status omits the screen.

Scripts can POST the same arguments to `/api/atv_bridge/tool/<name>` with the admin token. The bridge also supplies six text tools to Home Assistant’s LLM APIs. For Assist, expose each intended bridge media player. [Agent skills](skills/) · [MCP API](docs/mcp-api.md)

## Using tv-use with Home Assistant

Home Assistant’s built-in Apple TV integration provides media player and remote entities. tv-use provides its own entities, a live Chromium presentation, screen observation, and a room-aware MCP endpoint. Disable the built-in entry only for a TV you move to the bridge. To switch back, stop the bridge app and re-enable that entry. [Setup guide](docs/SETUP.md#prepare-each-tv)

## How it works

- **Control.** The app keeps a separate pyatv session for each registered TV. The staged fork includes 18 patches for AirPlay pairing and URL playback, Companion errors and keepalive, and other protocol fixes. [Patch series](docs/pyatv-fork.md)
- **Presentation.** The default is mirroring at 1920×1080 and 10 fps. One TV can mirror at a time by default. Pages can reach the public internet, the tailnet and the bridge’s stage. Home Assistant and other LAN services are blocked by default. Use the app option `page_network_allow` to grant access to a LAN dashboard. For HTML, files and bundles, `auto` mode can switch to MP4 playback after two failed mirror starts. Live URLs use mirroring. HA admin callers can use `tv_page_run` to call page functions, query the DOM and interact with live content. Set `page_scripts: false` to disable caller scripts. [Presentation options](app/DOCS.md)
- **Observation.** A separate observer process uses RemotePairing for accessibility text and screenshots. Results depend on what the current app exposes. Protected video may appear black in screenshots. [Screen-reading measurements](docs/live-accessibility.md)
- **Access.** HA authenticates agent requests and forwards them to the app on loopback port 8765. LAN port 8766 serves the watchdog, uploaded content and rendered media without authentication. The bridge has no purchase guard. Use the agent’s confirmation flow and tvOS purchase settings if you want approval before spending.

## Hardware results and limits

On the tested Home Assistant Green, two inline pages shared one mirror sender. Across 12 show/hide cycles, every hide returned the browser process count to zero. Two sender crashes recovered during that run. It was a single-TV test. Recheck each TV with `tv_status` after a tvOS update. [Hardware test results](docs/live-browser-lifecycle.md)

- Wake after a long overnight sleep is not yet measured. Multi-hour mirroring is also unverified.
- Built and tested with fakes, not yet exercised on a real TV: phone-reply pairing through a Home Assistant notification; `tv_pair` with `read_code_from_screen`; automatic handling of in-app profile pickers, App Tracking Transparency prompts and tvOS "Open in" dialogs; discovery after the TV's address changes; removing a TV; finishing Add Apple TV with Skip screen pairing; taking over another room's mirror with `replace: true`.

## Further reading

- [App development](app/README.md) and [integration development](custom_components/atv_bridge/README.md)
- [Architecture](docs/ARCHITECTURE.md) and [browser lifecycle measurements](docs/live-browser-lifecycle.md)
- [Render cost](docs/spike-s2.md) and [browser comparison](docs/spike-s2b.md)

## Acknowledgements

This project stands on other people's protocol work, reverse engineering and open-source software. Thank you.

### Protocol and device work

- [pyatv](https://github.com/postlund/pyatv) by Pierre Ståhl (postlund), MIT. The control layer for Companion, MRP, AirPlay and RAOP. The bridge ships a fork in `vendor/pyatv/`; see [docs/pyatv-fork.md](docs/pyatv-fork.md).
- The authors of the upstream pyatv pull requests carried in the fork: JaviSoto ([#2894](https://github.com/postlund/pyatv/pull/2894), AirPlay pairing on tvOS 27), jlacivita ([#2846](https://github.com/postlund/pyatv/pull/2846), URL playback on modern tvOS), robkochman (the [playback rework](https://github.com/postlund/pyatv/commit/8144c77c6cecbed4f9ba2adb5a350ad86a8f6604) it builds on), jullduxster ([#2948](https://github.com/postlund/pyatv/pull/2948), [#2947](https://github.com/postlund/pyatv/pull/2947)), pedro-f20 ([#2939](https://github.com/postlund/pyatv/pull/2939)), zmiyajan ([#2953](https://github.com/postlund/pyatv/pull/2953)), hkoeck ([#2935](https://github.com/postlund/pyatv/pull/2935)), MarvinSchenkel ([#2945](https://github.com/postlund/pyatv/pull/2945)), SeoFood ([#2925](https://github.com/postlund/pyatv/pull/2925), [#2924](https://github.com/postlund/pyatv/pull/2924)) and marlow-systems ([#2920](https://github.com/postlund/pyatv/pull/2920)).
- [tvpipe](https://github.com/mitsuhiko/tvpipe) by Armin Ronacher (mitsuhiko). The AirPlay mirroring sender. Its main library is Apache-2.0. The v0.2.0 release binary includes the LGPL-3.0-or-later FairPlay module and is distributed under LGPL-3.0-or-later terms. The app unpacks the release under `/opt/tvpipe/`. [Release licensing](https://github.com/mitsuhiko/tvpipe/blob/v0.2.0/README.md#license)
- [doubletake](https://github.com/omarroth/doubletake) by omarroth, LGPL-3.0-or-later. The reference open-source AirPlay mirroring sender and the source of tvpipe's FairPlay port; used for tvOS 27 protocol details and one of the credential formats the fork converts. It is not shipped.
- [pymobiledevice3](https://github.com/doronz88/pymobiledevice3) by doronz88, GPL-3.0-or-later. The RemotePairing tunnel, screenshots and accessibility access. Pinned, and run only in the observer's separate process.
- [ipb](https://github.com/ipbtools/ipb), MIT. The observer's accessibility archive codec is adapted from it. The license is in `app/atv_bridge_observer/licenses/`.
- [apple-protocols](https://github.com/basmilius/apple-protocols) by basmilius. A reference for the Companion session, extra commands, and reusing AirPlay credentials for Companion.
- [Unfolded Circle integration-appletv](https://github.com/unfoldedcircle/integration-appletv). Its runtime patches for tvOS 27 and its connection handling informed the fork and the wake logic.
- [appletv-mcp](https://github.com/trevor-nichols/appletv-mcp) by trevor-nichols. Prior art for an Apple TV MCP server; its rule never to replay an uncertain key press is used here.
- [UxPlay](https://github.com/FDH2/UxPlay) and [atvloadly](https://github.com/bitxeno/atvloadly), for their documentation of AirPlay mirroring and of Apple TV developer pairing from Linux.

### Software the app runs on

- [Home Assistant](https://www.home-assistant.io/), Apache-2.0, which hosts all of it, and its [Tailscale app](https://github.com/hassio-addons/app-tailscale) by Franck Nijhof, which carries agents' traffic and tailnet URLs.
- [Chromium](https://www.chromium.org/), which renders every page, and [Playwright](https://playwright.dev/), which drives it for URL-playback rendering.
- [FFmpeg](https://ffmpeg.org/), for the MP4 fallback.
- [Debian](https://www.debian.org/), the app's base image, and the [Noto fonts](https://fonts.google.com/noto).
- Python libraries: [aiohttp](https://github.com/aio-libs/aiohttp), the [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk), [python-zeroconf](https://github.com/python-zeroconf/python-zeroconf), [srptools](https://github.com/idlesign/srptools), [Pillow](https://github.com/python-pillow/Pillow), [PyYAML](https://github.com/yaml/pyyaml) and [jsonschema](https://github.com/python-jsonschema/jsonschema).

## Licenses

The bridge service is MIT. The observer (`app/atv_bridge_observer/`) is GPL-3.0-or-later and runs as its own process. The vendored pyatv fork keeps pyatv's MIT license. The installed tvpipe binary is under LGPL-3.0-or-later terms, as described above. Each other component keeps its own license.
