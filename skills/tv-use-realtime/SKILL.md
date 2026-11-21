---
name: tv-use-realtime
description: Run a live voice session on an Apple TV through tv-use, where the TV is the reply and the agent rarely speaks. Covers when to speak, showing progress on screen instead of narrating, tool latency and fast turns. Use when a user talks to an agent while watching the TV.
---

# Run a real-time session on the TV

The user talks and watches the TV. You act, and the screen answers.

## Stay quiet

- Never announce what you will do or what you did.
- Never read aloud what the screen shows.
- Show progress on screen, not in speech.
- Speak only when the screen cannot carry the message, in one sentence:
  - a choice or approval only the user can give: purchases, rentals, upgrades, sign-in, taking over another room's screen
  - a pairing code request (`data.ask_user`)
  - a failure the screen does not show
  - an answer that is not on screen
  - a step that runs much longer than usual with nothing new on screen

## Show, then fill

1. Show the page for the task at once, in its pending state, with a status line. Starting the mirror takes seconds; start it before the real work.
2. Fill it in place with `tv_page_run` as results arrive. Update the status line in the same call.
3. Clear the status line when done.

One page per task. Never show a new page for each change. Build pages with **tv-use-web-apps**.

The user can pick on screen with the Siri Remote: `tv_show` uses `remote: "tvos"` for a page you write and `"focus"` for a `url`. Read what they did with `tv.state().input` and act on it without asking.

## Latency

Local connection, from a Mac at home:

| Call | Time |
|---|---|
| `tv_status` | 0.07 s |
| `tv_page_run` | 0.03–0.7 s |
| `tv_look`, text | 0.6 s |
| `tv_look`, screenshot | 2.5 s |
| `tv_remote` burst | 2 s |
| `tv_show`, new page | 2.7–5.2 s |
| `tv_navigate`, three moves | 8 s |

- Use a local connection. A remote one adds a round trip to every call.
- Patch the page; do not re-show it.
- Read text, not screenshots. A screenshot also shows a toast on the TV.
- Send known keys as one `tv_remote` burst, or a `tv_sequence` with checkpoints.
- Leave 0.25 s between `tv_page_run` calls, or combine them.

## When the screen cannot show it

- **Someone is watching:** `tv_look` shows playback you did not start. Ask once before replacing it.
- **Recorded mode:** `tv_show` returned `data.transport: "video"`. The page cannot change; show the final page instead.
- **A native app is on screen:** there is no page. Let the app show the result and check it with `tv_look`.
- **`mirror_busy`:** ask before `replace: true`.
- **A call fails:** read `error.fix` and try it once, silently. If it fails again, say so in one sentence.

In these cases, one spoken sentence is the right fallback.

**tv-use-connect** has the tools and error codes.
