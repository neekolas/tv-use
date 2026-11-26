# Passing Siri Remote presses through to a mirrored page

**Question.** `tv_show` mirrors a browser page to the Apple TV through tvpipe. Can the bridge learn about each press on the Siri Remote during that mirror, so it can forward left, right, up, down and select into the page as key events?

**Status: source review plus one hardware probe. Verdict: go, for presses sent with `tv_remote`.** The physical Siri Remote is not yet tested.

Source references use bridge checkout `7a5e37b`. tvpipe references use tag `v0.2.0`, commit `c2456d29`. pymobiledevice3 references use version 11.61.0, the version locked in `app/uv.lock`. A claim marked *(speculation)* has no source behind it.

## Verdict

**Go, for presses sent with `tv_remote` (Companion).** The os_trace syslog relay over RSD shows one unredacted NOTICE line from `backboardd` (subsystem `com.apple.BackBoard`, category `Button`) per key down. The line names the key by HID page/usage: Left 0x1/0x8B, Right 0x1/0x8A, Up 0x1/0x8C, Down 0x1/0x8D, Select 0xC/0x80.

```
Unknown page:0x1 usage:0x8B downEvent:1 source:externalAccessory (MediaRemote) senderID:… down
```

Outcome against the pass bar below:

1. **Key identity: met.** Page/usage names each key. `downEvent:1` marks key down. The Select line starts with `Select`, not `Unknown`, so the matcher reads page, usage and `downEvent`.
2. **Shared process: met by process only.** The line comes from `backboardd`, not `rapportd`. Its source field shows the injected path.
3. **Coverage: met.** 20/20 presses in the main run, 5/5 in the narrowed run.
4. **Latency: met.** The line follows the Companion arrival line by about 2 ms on the TV clock. The host receives it a median of about 117–120 ms after the TV logs it. The clock offset between the TV and the Mac was not measured.
5. **No self-trigger: met.** 0 lines in the baseline and the `tv_look` control.

Limits:

- **Physical remotes behave differently from `tv_remote`, and both are now covered.** `tv_remote` (Companion) logs arrows on the generic desktop page (0x1/0x8A–0x8D) with `source:externalAccessory (MediaRemote)`. A physical clickpad Siri Remote on tvOS 27.2 logs arrows on the consumer page (`Left page:0xC usage:0x44`, Right 0x45, Up 0x42, Down 0x43) with `source:verifiedExternalAccessory`. Select is 0xC/0x80 on both. The matcher accepts both pages. See [Physical remotes](#physical-remotes).
- **A 1st-gen touch-surface Siri Remote has no directional click.** Every click, at the center or an edge, logs the same Select line. Swipes log no Button line. They appear only as DEBUG `TouchEvents` digitizer lines with a normalized finger position about every 15 ms. The bridge turns those into arrows with its own swipe recognizer.
- **Two TVs, two tvOS versions.** Apple TV 4K 1st gen on tvOS 26.6.1 and Apple TV 4K 2nd gen on tvOS 27.2. Log text can change between tvOS versions.

## Pass bar

This bar was set before any hardware result. A usable signal must meet all five points.

1. **Key identity.** It identifies which key was pressed.
2. **Shared process.** It comes from a process that the physical Siri Remote also goes through. A signal that exists only on the Companion path used by `tv_remote` fails.
3. **Coverage.** It fires on nearly every press.
4. **Latency.** It arrives within a few hundred milliseconds.
5. **No self-trigger.** The bridge's own screen reads do not trigger it.

## Paths

Paths that work during a mirror come first. Within that group the order is how many of the five keys the path could identify, then build cost. Paths that replace the mirror or carry no press come last. The probe tested paths 1, 2 and 9. For the `source-only` rows the order is still a hypothesis.

| # | Path | Keys it could deliver | Feasibility | Latency | Reliability | Build cost | Evidence |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | os_trace syslog relay over RSD | All five, by HID page/usage | High for `tv_remote` presses. Physical Siri Remote untested | About 120 ms at the host | 25/25 presses. Log text can change between tvOS versions | Small | `tested`, passes |
| 2 | DVT `activitytracetap` | Same lines as path 1, plus format strings | Works, but adds nothing over path 1 | About 60 ms later than path 1 | Same as path 1. Parser marked "less stable" upstream | Small to medium | `tested`, adds nothing |
| 3 | DVT kdebug, IOHID class | A press with no key name. Arguments may map to a key *(speculation)* | Low to medium | About 200 ms floor | Likely differs between Companion and the physical Siri Remote | Medium | `source-only` |
| 4 | AirPlay 2 event channel, passive | Unknown. Observed command types are housekeeping only | Low for presses *(speculation)*. tvpipe already connects the channel | Under 50 ms | High if present. Ordered TCP, each command acknowledged | None. Run tvpipe with `-v` | `source-only` |
| 5 | AirPlay 2 event channel with advertised MR commands | Play/Pause most likely. Left and right only if the mirror UI maps them to skip *(speculation)*. No up, down or select | Medium for Play/Pause, low for left and right. Unknown for a mirror session | 50–150 ms | Unknown. Media commands are not guaranteed per press | 3–5 days. Fork tvpipe | `source-only` |
| 6 | MRP tunnel | Play/Pause as a state change. None of the five keys | Low to medium | 100–300 ms | Poor. A state change is not a press | Listen: none | `source-only` |
| 7 | Companion events | None directly | Very low | 100–300 ms | Poor | Listen: none | `source-only` |
| 8 | DACP/RAOP callback | Play/Pause, perhaps volume. None of the five keys | Low. Needs a second RAOP session that may end the mirror | 50–200 ms | Unknown | 1–2 days | `source-only` |
| 9 | URL playback seek inference (`play_url`) | Left and right as ±10 s seeks, select as pause. No up or down | Works for those signals. **Replaces the mirror**, so it cannot drive a live page | Jump visible in `screen.playing.position_s` on a read 2 s after the press, not on the read right after it | Medium. Seeks are inferred | 3–5 days for a slideshow mode | `tested` |
| 10 | AX audit notifications | None. Reports focus changes | Low | n/a | A fullscreen mirror moves no focus | n/a | `source-only` |
| 11 | notification_proxy | None known. No payload before tvOS 27.2 | Low. Needs an exact notification name; none known | Low if a name existed | n/a | n/a | `source-only` |
| 12 | Other AirPlay-side: MRP HID or game-controller registration, CarPlay-style HID reports, Sidecar, MediaDataControl | None found | Very low | n/a | n/a | Do not build | `source-only` |
| 13 | Other RSD services: pcapd, BT packet logger, sysmontap, graphics, springboardservices | None | Very low | n/a | n/a | n/a | `source-only` |

Only paths 1–3 could name up, down and select. They are side channels, not documented events. Path 1 names all five on hardware.

Near-misses in the same capture: a `PineBoard` DEBUG line gives the usage for arrows only (16/20, none for Select). `TVAirPlay` logs each press without key identity. `rapportd` logs `Command <Left>, Button Down`, which is Companion only and fails pass bar point 2.

## Path details

### 1. os_trace syslog relay

- The service is `com.apple.os_trace_relay.shim.remote`. It pushes one record per log message with no client-side batching. Each record has pid, process path, sender image, level, subsystem, category, message and device time. See pymobiledevice3 `services/os_trace.py`.
- The client can request debug and info levels through `StreamFlags`. A `Pid` filter runs on the device and cuts volume.
- **It depends on a log line that no public source lists.** The probe found one in `backboardd` on tvOS 26.6.1. The bridge supports tvOS 27 or later (`docs/live-accessibility.md:41`), so the line must be confirmed again there.
- Arguments marked private arrive as `<private>`. Showing them needs a logging profile approved on the device, which a shipped bridge cannot rely on. See Apple developer forum threads [705810](https://developer.apple.com/forums/thread/705810) and [780682](https://developer.apple.com/forums/thread/780682). The `backboardd` line is not redacted.

### 2. DVT `activitytracetap`

- It reads the same log store as path 1, so it shows no line that path 1 lacks. Its gain is the format string, typed arguments and a device-side NSPredicate. See `services/dvt/instruments/activity_trace_tap.py`.
- Delivery is batched. Measured: the line arrives about 60 ms after path 1.

### 3. DVT kdebug, IOHID class

- `coreprofilesessiontap` can filter kdebug by class. The macOS SDK lists `DBG_IOKIT 5` and `DBG_IOHID 35`. macOS `trace.codes` lists `HID_DispatchKeyboard`, `HID_HandleReport` and `HID_DispatchHIDEvent`.
- It is the only path that could see a press with no log line. It carries arguments, not a key name.
- Companion presses are injected in user space and may never reach kernel IOHID *(speculation)*. A null result with `tv_remote` does not rule this path out.

### 4 and 5. AirPlay 2 event channel

- The receiver returns `eventPort` in SETUP. tvpipe connects and answers each `POST /command` with `200 OK`. It knows `updateInfo`, `updateTimingPeerInfo` and `forceKeyFrame`. Any other type becomes `Event::Other` and is logged at debug level, type only. See tvpipe `crates/tvpipe/src/airplay/events.rs:29–56`, `session.rs:769–781`.
- **The tvpipe mirroring path has no remote-input callback.** It never sends a `/command` itself. doubletake behaves the same way (`internal/airplay/event_channel.go:268–335`).
- tvpipe's own hardware capture holds one event command, `updateInfo` (`crates/tvpipe/tests/vectors/airplay-mirror/appletv-session.txt:148`). That is tvpipe's observation. Nobody logged the channel while a person pressed keys.
- **A receiver-to-sender command exists in AirPlay 2.** shairport-sync 5.6 sends `sendMediaRemoteCommand` on the event channel after the sender posts `updateMRSupportedCommands`, `updateMRNowPlayingInfo` and `updateMRPlaybackState`. See shairport-sync `remote/remote.c:190–223`, `rtsp.c:2051–2069`. The project calls it "still a work in progress".
- The command numbers are media commands: play, pause, play/pause, stop, next, previous, fast forward, rewind (`remote/remote.c:33–48`). **No number stands for up, down, left, right or select.**
- The evidence is from audio sessions. Whether tvOS sends the command for a screen-mirroring session is unknown. Whether the mirror UI maps left and right to skip is unknown *(speculation: it does not)*.
- No Apple document was found that describes a way for an AirPlay sender to receive clickpad presses as raw button events. During video playback the Apple TV may treat a press as skip backward or forward. A native sender handles media commands through `MPRemoteCommandCenter`, and each press is not guaranteed to arrive ([forum thread 4036](https://developer.apple.com/forums/thread/4036)).

### 6. MRP tunnel

- The bridge already holds this tunnel when AirPlay credentials exist (`app/atv_bridge_app/atv/__init__.py:221–242`). pyatv is the MRP client and the TV is the origin. The TV pushes its state; pyatv sends commands.
- MRP defines `SEND_COMMAND_MESSAGE` with play, pause, skip and seek commands (`vendor/pyatv/pyatv/protocols/mrp/protobuf/CommandInfo.proto:5–62`). pyatv only creates this message to send it. It registers no handler for an inbound one.
- At best a Play/Pause press shows as a state change, and only if the mirror session is a now-playing client *(speculation)*.

### 7. Companion events

- The fork listens to `_iMC`, `SystemStatus`, `TVSystemStatus`, `_tiStarted` and `_tiStopped` (`vendor/pyatv/pyatv/protocols/companion/__init__.py:240–244, 434, 499–502, 584`). They report media flags and system state.
- `_hidC` (keys) and `_hidT` (touch) are requests from the client to the TV (`api.py:319, 329`). No source shows the TV sending physical remote input to a subscribed client.

### 8. DACP/RAOP

- The receiver calls `GET /ctrl-int/1/<cmd>` on a `_dacp._tcp` service that the sender advertises. Commands include `playpause`, `nextitem`, `volumeup` ([openairplay spec](https://openairplay.github.io/airplay-spec/audio/remote_control.html)). No command stands for a direction or select.
- pyatv sends the `DACP-ID` and `Active-Remote` headers but serves no callback (`vendor/pyatv/pyatv/support/rtsp.py:270–272`). tvpipe sends neither header in mirroring (tvpipe `docs/airplay-mirroring.md:285`).
- shairport-sync uses DACP for classic AirPlay and the event channel for AirPlay 2 (`remote/remote.c:544–565`). Whether a current tvOS receiver still browses `_dacp._tcp` is unknown *(speculation: unlikely)*.

### 9. URL playback seek inference (`play_url`)

- In this mode the tvOS player owns the remote. Left or right skips 10 seconds, the center plays or pauses, and up and down show controls ([Apple TV User Guide](https://support.apple.com/guide/tv/atvb7944597f/tvos)).
- The fork handles `playbackState` events on the event channel (`vendor/pyatv/pyatv/protocols/raop/protocols/airplayv2.py:197–217`). The probe confirmed that left and right move `screen.playing.position_s` by 10 s and that select pauses.
- **A seek jump cannot be told apart from scrubbing or from another controller.** Up and down are invisible to the sender.
- `play_url` plays a rendered MP4 (`docs/ARCHITECTURE.md:68`). It cannot drive a live page. It may suit a slideshow with one slide per 10 s *(speculation)*.

### 10. AX audit notifications

- The host callbacks report focus, element and app changes. None is a key event. The callback list comes from iOS 18.6 simulator binaries, so it is indicative for tvOS, not proof.
- An earlier tvOS 27.2 run requested event mask 2. The daemon changed it back to 1 and gave no focus result after Back and Up (`docs/live-accessibility.md:14`). That run tested focus on Settings, not a mirror.
- The mirrored page is one video surface. A press there moves no tvOS focus, so no notification is expected.

### 11. notification_proxy

- `com.apple.mobile.notification_proxy.shim.remote` observes Darwin notifications by exact name only. No name that fires per press is known. Before tvOS 27.2 a notification carries no payload, so it gives no key identity there.

### 12 and 13. Other paths

- **MRP HID and game-controller messages** exist in `ProtocolMessage.proto:85–99`. pyatv only sends them to the TV. They let a client act as an input device *(speculation)*.
- **MediaDataControl** carries audio statistics (`raup`, `rrmt`) from the TV (tvpipe `mdc.rs:84–112`). **CarPlay-style HID reports** have no source. **Sidecar** is not offered by tvOS.
- **pcapd** sees IP packets. Bluetooth HID is not IP and Companion traffic is encrypted. **BT packet logger** has no RSD shim in pymobiledevice3 and needs a logging profile. The probe TV does list a shim for it; it was not tried. **sysmontap, graphics and springboardservices** carry no input.

## apple-protocols.dev review

[apple-protocols.dev](https://apple-protocols.dev) documents one TypeScript client library, `@apple-protocols/*`. The library controls an Apple TV over AirPlay 2, MRP, RAOP and Companion Link. **The review found no new path and changes no rating in the table.**

All 41 pages were read through the site's full-text export, `llms-full.txt`. Facts marked *(repo)* come from the linked [repository](https://github.com/basmilius/apple-protocols) at commit `3431c380`, not from the site.

What the site does not cover:

- **Screen mirroring.** The word "mirror" appears on no page and nowhere in the library source.
- **RSD, lockdown and developer services.** No page. Paths 1–3, 10, 11 and 13 are not affected.
- **DACP callbacks.** The site lists `dacpId` and `activeRemoteId` as session headers only ([reference](https://apple-protocols.dev/reference/airplay)).
- **`sendMediaRemoteCommand` and `updateMRSupportedCommands`.** Neither name appears on the site or in the source. Path 5 still rests on shairport-sync alone.

What the site documents that bears on the question:

- **Path 4, event channel.** The receiver sends `POST /command` and the client answers `200 OK` ([AirPlay sessions and channels](https://apple-protocols.dev/protocols/airplay)). The known command types are `duckAudio`, `unduckAudio`, `cycleUsePickedRoute`, `died` and the play-queue commands `playbackState`, `notification` and `unhandledURL` *(repo: `channels/event.ts`, `playback/playQueue.ts`)*. All are housekeeping. The library opens this channel for the MRP tunnel and for URL playback, never in a mirror session.
- **Path 6, MRP.** The TV pushes now-playing state and supported commands. The client sends commands ([Media Remote](https://apple-protocols.dev/protocols/mrp)). The library handles no command sent by the TV *(repo: `mrp/codec.ts`)*. This is a second client that agrees with pyatv.
- **Path 7, Companion events.** The library registers interest in `_iMC`, `SystemStatus`, `TVSystemStatus`, `NowPlayingInfo` and `SupportedActions` *(repo: `companion-link/src/session.ts:33`)*. The last two are not in the fork's list. They carry now-playing state, not a press. `_hidC`, `_hidT`, `_touchC` and the game controller event `_gcC` all go from the client to the TV ([Companion Link](https://apple-protocols.dev/protocols/companion-link)).
- **Path 12, MRP HID.** HID events go from the client to the TV and the TV never replies. tvOS drops the MRP session when it gets a virtual touch event ([Media Remote](https://apple-protocols.dev/protocols/mrp)). `registerHIDDevice` makes the client an input device *(repo: `mrp/messages.ts:348–372`)*. It does not subscribe the client to the TV's input. This supports "Do not build".
- **Path 12, MRP game controller.** The protos exist, with `dPadX`, `dPadY`, `a` and `pause` fields. The library sends none of them. The source does not show which side sends them *(repo: `proto/GameControllerMessage.proto`, `RegisterForGameControllerEventsMessage.proto`)*.
- **AirPlay feature bits.** The library names 44 bits *(repo: `airplayFeatures.ts`)*. None names HID, input or touch forwarding. The source says a device needs `SupportsHangdogRemoteControl` to receive HID events. The bit means the TV accepts input, not that it sends any.
- **SETUP options.** The session that carries the MRP tunnel sends `isRemoteControlOnly: true` and a type-130 stream, as pyatv does *(repo: `session.ts:248–251, 507–516`)*. No option asks the TV to forward input.
- **MRP update subscription.** The client can subscribe to artwork, now-playing, volume, keyboard, output device and system endpoint updates *(repo: `proto/ClientUpdatesConfigMessage.proto`)*. No flag concerns input.
- **`FetchSiriRemoteInfo`.** The only API on the site that names the Siri Remote is a Companion request with one reply ([reference](https://apple-protocols.dev/reference/companion-link)). The client polls it. The TV does not push it.

Two tools in the repository could help a later test. Neither was run.

- **Man-in-the-middle proxy.** The [diagnostics](https://apple-protocols.dev/diagnostics) app has a proxy that sits between a controller and the TV. It answers the controller's pairing, connects to the TV with stored credentials, and logs the decrypted control channel, event channel and data channel *(repo: `packages/diagnostics/src/main/proxy/airplay/proxy.ts:33–38`)*. Path 5 lacks a capture from an Apple sender. With an iPhone as the controller, this proxy could record the `updateMRSupportedCommands` body and any `sendMediaRemoteCommand` that tvOS sends back. It does not relay audio or timing. It treats every data stream as MRP, so an iPhone mirror through it will most likely fail without a patch *(speculation)*.
- **Traffic sink.** The library reports every decoded message to a traffic sink ([Transport and logging](https://apple-protocols.dev/protocols/transport)), and the diagnostics CLI prints all traffic since a mark. It could log the MRP tunnel and Companion next to a mirror while a person presses keys. It covers paths 6 and 7 with every MRP type decoded. It does not cover the event channel of the mirror session, which tvpipe owns.

## Companion presses versus the physical remote

The hardware probe sends presses with `tv_remote`. That tool uses Companion `_hidC` (`vendor/pyatv/pyatv/protocols/companion/api.py:319`). The physical Siri Remote enters tvOS through a different path. No primary source for the tvOS internals was found, so this split is an assumption.

| Stage | Physical Siri Remote | Companion `tv_remote` |
| --- | --- | --- |
| Entry | Bluetooth LE → Bluetooth daemons → HID service | TCP Companion → `rapportd` and the TV remote daemon → virtual HID event |
| Kernel IOHID | Possible | Unlikely |
| `backboardd` dispatch, shell, focused app | Assumed. Not tested | Yes. Observed |

Effect on each path:

- **Paths 1 and 2.** A line from `rapportd` or a Companion daemon is a false positive under pass bar point 2. A line from `backboardd`, `PineBoard`, the AirPlay receiver UI or UIKit is the useful kind. A line from `bluetoothd` cannot appear with `tv_remote` at all.
- **Path 3.** Most likely to differ. See above.
- **Paths 4–9.** Not assessed. The sources do not say whether tvOS routes media commands the same way for both press sources.
- **Paths 10–13.** They carry no press from either source.

`tv_remote` ends with one screen read (`docs/ARCHITECTURE.md:51`), so the probe ran `tv_look` alone as a control. During the mirror, `backboardd` sends each press to `TVAirPlay`, which drops it (`firstResponder (null)`). Back ends the mirror.

The physical Siri Remote is the open check. Run the one-minute physical check in the Verdict.

## Physical remotes

These runs used a person pressing the real remote on November 20, 2026.

| TV and remote | Action | Log line | Result in the page |
| --- | --- | --- | --- |
| Living Room, tvOS 27.2, clickpad | Ring clicks | `Left page:0xC usage:0x44 downEvent:1 source:verifiedExternalAccessory …` (Right 0x45, Up 0x42, Down 0x43) | Rejected by the first matcher. Matched after commit "Match physical clickpad arrows on the consumer page". |
| Living Room, tvOS 27.2, clickpad | Center click | `Select page:0xC usage:0x80 …` | Enter, verified end to end. |
| Bedroom, tvOS 26.6.1, 1st-gen touch | Clicks at center and four edges | The same `Select page:0xC usage:0x80` line each time | 5 trusted Enter presses, about 165 ms from the first backboardd line to the page keydown. |
| Bedroom, tvOS 26.6.1, 1st-gen touch | Swipes and a slow drag | No Button line. DEBUG `TouchEvents` lines: `touchpad Digitizer token: …; down|move|up; … {x, y}`, x and y from 0 to 1, y down | Arrows only through the swipe recognizer. Verified by replaying the capture, not yet by a live swipe. |
| Both | Menu | `Menu page:0x1 usage:0x86 …` to the system | Not forwarded. Menu ends the mirror. |

The stream needs the DEBUG flag (PAYLOAD|DEBUG, 0x24) for the touch lines. With the backboardd pid filter it carried about 65 lines per second while a finger touched the pad and almost nothing otherwise.

## Other signals in the remote captures

The saved tvOS 26.6.1 captures also recorded events that can explain a missed press or a frozen mirror.

| Process | Observed signal | What it can establish |
| --- | --- | --- |
| `backboardd` | All 40 `SendHIDEvent` delivery records from the controlled mirror run named `TVAirPlay`. The physical-remote capture also contained deliveries to `PineBoard`. | Which process received input. Whether it acted requires a separate observation. |
| `TVSystemUIService` | A banner moved through presentation, dismissal and timeout. | When a system banner appeared and disappeared. |
| `TVAirPlay` | Receiver-side audio underruns and video frame-processing errors. | Whether the receiver encountered playback problems while the sender was transmitting. |
| `PineBoard` | A configured 3840 × 2160, 59.94 Hz HDR10 display mode, Atmos preference and hardware flags, and mute state. | Display and audio configuration. The observed output supplied no numeric volume. |

The production button watcher filters for `backboardd`. The other signals came from broader captures. Their availability on tvOS 27.2 still needs verification.

## Hardware results

Run on 2026-11-20. The Verdict holds the usage values and the limits.

**Setup.** The Mac lab connected over RSD with its own RemotePairing record. The bridge showed an HTML page with `tv_show` (`transport: mirror`, `verified: true`, app `com.apple.TVAirPlay`) and sent every press with `tv_remote`.

**Main run.** 190 s, streams `syslog` and `activity`, all levels. Phases: 15 s baseline, one `tv_look` control, 20 presses about 4.6 s apart. Volume: 151,727 syslog records (about 800 per second) and 261,382 activity records.

| Key | Left | Right | Up | Down | Select |
| --- | --- | --- | --- | --- | --- |
| Down lines / presses | 6/6 | 4/4 | 4/4 | 2/2 | 4/4 |

- Each press gave 4 `Button` lines: down, up, `began` and `finished`. No `Button` line fell outside a press.
- Delay from the first `rapportd` `Received requestID '_hidC'` line to the down line, TV clock: min 0.2, median 2.0, max 13.6 ms.
- Host receive delay for the down line: median 120 ms on `syslog` (105–472), 181 ms on `activity`.

**Narrowed run.** `StreamFlags` 0x4 and a device-side pid filter for `backboardd`. The probe finds the pid by name through `OsTraceService.get_pid_list`; the pid changes on reboot. 5 presses, one per key, all seen. The stream carried 0 records while idle and 75 records in 50 s (1.5 per second). Host receive delay: median 117 ms (103–135).

**RSD services.** The TV lists 59. They include `com.apple.os_trace_relay.shim.remote`, `com.apple.instruments.dtservicehub`, `com.apple.mobile.notification_proxy.shim.remote` and `com.apple.bluetooth.BTPacketLogger.shim.remote`.

**`play_url`.** Each read came 2 s after the press. Left moved `screen.playing.position_s` from 7 to 2. Two right presses moved it from 5 to 17 and from 20 to 33. Select set the state to `paused`.

**Not done.** kdebug (path 3), the physical Siri Remote, long press, tvOS 27, and a stream from the bridge's own pairing identity.

**Evidence.** The captures are in `spikes/live/results/`, which is gitignored. The probe is `spikes/live/atvlab/press_probe.py`. Narrowed run:

```sh
uv run --no-sync atvlab press-probe --seconds 50 --streams syslog --syslog-flags 0x4 --syslog-process backboardd --quiet
```

The main run used `--seconds 190 --streams syslog,activity`. Analysis: take the `backboardd` `Button` records with `downEvent:1`, match each to a press by the `rapportd` arrival line, and tally by page/usage. The probe's `--analyze` hides the usage for this line, so the tally was done on the raw records.

## Sources

Bridge checkout:

- `docs/ARCHITECTURE.md:51, 62–68`, `docs/live-accessibility.md:14, 41`, `docs/pyatv-fork.md`
- `app/atv_bridge_app/atv/__init__.py:221–242`
- `vendor/pyatv/pyatv/protocols/companion/api.py:275–287, 319, 329`, `__init__.py:240–244, 434, 499–502, 584`
- `vendor/pyatv/pyatv/protocols/mrp/protobuf/ProtocolMessage.proto:85–99`, `CommandInfo.proto:5–62`, `messages.py:153–163`
- `vendor/pyatv/pyatv/protocols/raop/protocols/airplayv2.py:197–217`
- `vendor/pyatv/pyatv/support/rtsp.py:88–89, 270–272`
- `spikes/live/atvlab/press_probe.py` (commit `7d8c160`)

Other projects:

- [tvpipe v0.2.0](https://github.com/mitsuhiko/tvpipe/tree/v0.2.0), commit `c2456d29`: `docs/airplay-mirroring.md:285, 1205–1232`, `crates/tvpipe/src/airplay/events.rs`, `session.rs:769–781`, `mdc.rs`, `crates/tvpipe/tests/vectors/airplay-mirror/appletv-session.txt:148`
- [doubletake v0.6.0](https://github.com/omarroth/doubletake/tree/v0.6.0), commit `be27776b`: `internal/airplay/event_channel.go:268–335`
- [shairport-sync 5.6](https://github.com/mikebrady/shairport-sync/tree/5.6), commit `9118bbc`: `remote/remote.c:33–48, 190–223, 544–565`, `ap2_event_receiver.c:97–117`, `rtsp.c:2051–2069`. [Release notes](https://github.com/mikebrady/shairport-sync/releases)
- [apple-protocols.dev](https://apple-protocols.dev) (`llms-full.txt`, all pages) and [basmilius/apple-protocols](https://github.com/basmilius/apple-protocols), commit `3431c380`: `packages/airplay/src/channels/event.ts:64–101`, `session.ts:248–251, 507–516`, `playback/playQueue.ts:203–219`, `airplayFeatures.ts:8–51`, `mrp/codec.ts:86–152`, `mrp/messages.ts:348–372`, `packages/airplay/proto/GameControllerMessage.proto`, `RegisterForGameControllerEventsMessage.proto`, `ClientUpdatesConfigMessage.proto`, `packages/companion-link/src/session.ts:33`, `state.ts:17`, `messages.ts:98–129, 195–197, 285–293`, `packages/diagnostics/src/main/proxy/airplay/proxy.ts:33–38, 161–227`
- pymobiledevice3 11.61.0: `services/os_trace.py`, `services/notification_proxy.py`, `services/accessibilityaudit.py`, `services/pcapd.py`, `services/bt_packet_logger.py`, `services/dvt/instruments/activity_trace_tap.py`, `core_profile_session_tap.py`
- macOS SDK `usr/include/sys/kdebug.h`; `/usr/share/misc/trace.codes`
- Lab dumps of `axauditd` and `AccessibilityAudit` methods from iOS 18.6 simulator binaries (`/tmp/atvlab/`, not in the repository)

Web:

- [openairplay spec, Remote Control](https://openairplay.github.io/airplay-spec/audio/remote_control.html)
- [Apple TV User Guide, "Control what's playing"](https://support.apple.com/guide/tv/atvb7944597f/tvos)
- [Apple developer forums 4036](https://developer.apple.com/forums/thread/4036): presses not reaching `MPRemoteCommandCenter` during AirPlay
- Apple developer forums [705810](https://developer.apple.com/forums/thread/705810), [705868](https://developer.apple.com/forums/thread/705868), [780682](https://developer.apple.com/forums/thread/780682): private data in the system log
- [Detecting Button Presses and Gestures](https://developer.apple.com/library/archive/documentation/General/Conceptual/AppleTV_PG/DetectingButtonPressesandGestures.html): presses go through the responder chain of the focused app

Not found: any Apple document or reverse-engineering report that describes Siri Remote behaviour during a mirroring session.
