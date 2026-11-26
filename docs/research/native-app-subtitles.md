# Subtitle controls in native tvOS apps

Tested on a second-generation Apple TV 4K running tvOS 27.2 on November 25, 2026. The bridge used its existing pairing. Both players ran on the Apple TV.

## Companion

Apple’s TVRemoteCore sender constructs a `ToggleCaptions` request with a `CaptionsEnabled` string. Calling the macOS 27.2.1 framework’s request builder with a local receiver recorded `{"CaptionsEnabled":"YES"}` for on and `{"CaptionsEnabled":"NO"}` for off. The vendored `CompanionExtras.toggle_captions()` sends an empty dictionary. The earlier AirPlay experiment tested that empty request. [Vendored method](../../vendor/pyatv/pyatv/protocols/companion/extras.py) · [Previous experiment](../pyatv-fork.md#captions-on-a-live-player)

On the Apple TV app’s *12 12 12* trailer, the native player started with English selected. After `NO`, MediaRemote returned zero selected subtitle tracks. After `YES`, English returned. The numeric Companion setting `_mcs` read 3 in all three states. An SDK can use active-track readback to verify this command.

In Netflix, the bridge sent the same request during *Nate Bargatze: Good For Nathan*. Netflix’s own playback controls showed **English [Original]** checked and **English [Original] with Subtitles** unchecked before and after the on request. The receiver acknowledged the command. A second check opened the full subtitle picker after the on request. It still marked **Off** as selected. Manually choosing **English (CC)** changed the checked track, and choosing **Off** restored the original selection. The audio language remained English throughout. Screenshots of this protected player were black.

## MediaRemote

A playback queue request with `includeLanguageOptions` asks the active player for available and selected tracks. Language options with type 1 are subtitles. Commands 53 and 54 request enabling and disabling a specific language option. Apple’s public MediaPlayer framework lets each app register handlers and publish its available language groups. AVFoundation’s selection API acts on a player item owned by the app. [Apple’s language command](https://developer.apple.com/documentation/mediaplayer/mpremotecommandcenter/enablelanguageoptioncommand) · [Available language groups](https://developer.apple.com/documentation/mediaplayer/mpnowplayinginfopropertyavailablelanguageoptions) · [AVFoundation media selection](https://developer.apple.com/documentation/avfoundation/selecting-subtitles-and-alternative-audio-tracks)

Apple’s trailer advertised both commands and a queue item with distinct English and English CC tracks. Netflix advertised Play and Pause, omitted 53 and 54, and returned zero items in both the default queue request and a request explicitly addressed to its player. These measurements cover one title in each app on this TV. [Command IDs](../../vendor/pyatv/pyatv/protocols/mrp/protobuf/CommandInfo.proto) · [Command options](../../vendor/pyatv/pyatv/protocols/mrp/protobuf/CommandOptions.proto)

### The per-track payload that worked

Field 27, `CommandOptions.languageOption`, takes bytes. Sending the raw `LanguageOption` protobuf for the trailer’s English CC track produced transport success and player handler status 2 (`CommandFailed`). English stayed selected.

Apple’s installed MediaRemote framework produced two different representations of the same option. Its `data` property matched the vendored protobuf byte for byte. Its `MRLanguageOptionCreateExternalRepresentation` function produced a binary property list containing an `NSKeyedArchiver` archive of an `MRLanguageOption`. The archive retained the type, language tag, display name, characteristics, identifier and nested protobuf. The framework decoded an archive generated from that format back to the original test track.

Using the archived option in field 27, addressed to the active player, produced the following results. Each operation returned handler status 0 and was checked with a fresh request for the current tracks.

| Action on Apple’s trailer | Selected subtitle reported by the player |
| --- | --- |
| Starting state | English |
| 53 with the advertised English CC option | English CC |
| 54 with that English CC option | None |
| 53 with the saved original option | English |

## Netflix through the existing MCP

Press Up with `tv_remote` to reveal playback controls. Read `tv_look` for the actual title’s labels. This stream offered a pair of quick choices and an **Other subtitle and audio options** picker. Selecting **English (CC)** or **Off** in that picker avoids changing the audio language. Reopening the picker put focus on **Off** even when **English (CC)** was checked. The diagnostic read `UIAccessibilityTraitSelected` to distinguish the checked choice from keyboard focus. The public MCP now exposes that trait as `tv_look`’s per-item `selected` field. Netflix documents the same path into the picker. [Netflix player instructions](https://help.netflix.com/en/node/372) · [MCP remote and navigation](../mcp-api.md#tv_navigate)

In the live MCP test, selecting **English (CC)** from **Off** sent Down and Select and closed the picker. Reopening it showed **English (CC)** selected with focus on **Off**. Repeating the request returned `verified: true` with an empty path. Selecting **Off** restored the original subtitle and left **English [Original]** audio selected.

Netflix also retained an old subtitle table in its accessibility tree while the player controls had focus. Even a hit test at the center of a retained row could point to that row. In the open picker, the focused row and option belonged to the same `UITableView`. After dismissal, the focused button belonged to the player controls and the retained option belonged to a separate table. The bridge checks the owning container as well as the hit test for `ensure_selected`.

## Other control surfaces

Apple TV Remote’s **Set Captions** action in iOS Shortcuts gives an iPhone user a one-action control. Its behavior in Netflix on this TV remains untested. Apple also documents **Type to Siri** on Apple TV 4K. Our MCP can set text in a focused tvOS keyboard field. Whether Siri exposes the field to the bridge and whether the player honors a typed subtitle request are separate checks. [Example on shortcut](https://www.icloud.com/shortcuts/c72b72d14d40423fa9bb567c674c492b) · [Example off shortcut](https://www.icloud.com/shortcuts/65e5d58b18cb43818be7b484ef06b34e) · [Apple TV accessibility features](https://support.apple.com/en-ca/guide/tv/atvbaeff85db/tvos)

## SDK recommendation

Expose one idempotent `tv_subtitles` action with on/off and an optional language. For players that advertise the language commands, select the requested legible track or disable the current one, then read the track list again. For a simple on/off request, the corrected Companion payload is another option if the app provides independent readback. Route Netflix to its picker and verify the selected accessibility trait there. Preserve the audio selection and original playback state. Report an unverified result when the player offers no observable selection.
