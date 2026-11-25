# pyatv fork for the Apple TV bridge

`vendor/pyatv/` contains installable source for **0.18.0+atvbridge.2**. The upstream base is tag `v0.18.0`, commit `d88abc960e188d3cd2498d19117e75d7711d8600`. The 20 diffs in `vendor/pyatv-patches/` apply in filename order. The fork requires Python 3.11 or newer. Current zeroconf and miniaudio releases require that minimum. The bridge supports tvOS 27 or later. AppleTV11,1 running tvOS 27.2 (24K122) was the hardware reference. The fork's automated checks use simulated receivers and loopback sockets. [Upstream base](https://github.com/postlund/pyatv/tree/d88abc960e188d3cd2498d19117e75d7711d8600)

## Patch series

| Patch | Origin | Change and merge notes |
| --- | --- | --- |
| 0001 | [PR 2894](https://github.com/postlund/pyatv/pull/2894) | Sends the configured client name on HAP PIN start and every pair-setup request. Applied cleanly. |
| 0002 | [PR 2846 at 8848ad3](https://github.com/postlund/pyatv/pull/2846), [playback rework at 8144c77](https://github.com/postlund/pyatv/commit/8144c77c6cecbed4f9ba2adb5a350ad86a8f6604) | Uses the rework at the base of PR 2948 to integrate 2846's type-130 control stream, queue insertion and playback events. Keeps stream IDs per session, checks command responses, and preserves AirPlay 1 polling. Kept upstream `plistlib`, omitted the HTTP dumps, dependency edits and unrelated proxy changes. Retained the newer HAP user agent and replaced the invalid tuple raise with an authentication error. Added PTP peer negotiation for URL playback and resource teardown. |
| 0003 | [PR 2948](https://github.com/postlund/pyatv/pull/2948) | Applied the FCUP commit `b77759d2` on top of its playback base. Answers an `unhandledURL` event over the URL control stream with the original message and request IDs. Caps fetches at 8 MiB and 20 seconds. |
| 0004 | [PR 2939](https://github.com/postlund/pyatv/pull/2939) | Raises on Companion code-only replies. Extended the check to an underlying `_eu` error without `_em` or `_ec`. |
| 0005 | [PR 2953](https://github.com/postlund/pyatv/pull/2953) | Uses the client from `SET_NOW_PLAYING_PLAYER_MESSAGE` when tvOS omits the explicit now-playing-client message. Applied cleanly. |
| 0006 | [PR 2935](https://github.com/postlund/pyatv/pull/2935) | Stops Companion from publishing an unknown volume as zero, which caused RAOP to stream at −144 dB. Applied cleanly. |
| 0007 | [PR 2945](https://github.com/postlund/pyatv/pull/2945) | Closes the failing protocol and previously connected protocols on a partial connection failure. Applied cleanly. |
| 0008 | [PR 2925](https://github.com/postlund/pyatv/pull/2925) | Spaces touch events and clamps swipe coordinates between the endpoints. Applied cleanly. |
| 0009 | [PR 2924](https://github.com/postlund/pyatv/pull/2924) | Preserves credential, authentication and backoff exceptions from AirPlay remote-control setup. Applied cleanly. |
| 0010 | [PR 2920](https://github.com/postlund/pyatv/pull/2920) | Checks error TLVs during persistent and transient HAP setup. Merged M2/M4/M6 checks by hand around 0001's per-client headers. Kept pair-verify errors distinct from pair-setup errors. |
| 0011 | Fork alternative to [PR 2947](https://github.com/postlund/pyatv/pull/2947) | Declares `srptools>=1,<2` in the install requirements. The upstream test lock already specifies 1.0.1. |
| 0012 | Fork | Configurable Companion NoOp timer, TCP keepalive, failed-write handling and fake-device tests. |
| 0013 | Fork | Sends source version `715.2`. Reuses the existing public identity settings and verifies their wire values across two connections. |
| 0014 | Fork | Adds the public Companion extras module, guarded commands and scoped event subscriptions. Releases pending exchanges on disconnect and tests `_eu`-only errors. |
| 0015 | Fork | Converts persistent HAP pairings between pyatv, tvpipe and doubletake. Tests the external encodings and verifies a converted pairing against a fake Companion receiver. |
| 0016 | Fork | Sets the PEP 440 local version used by the library, wheel and CLI. |
| 0017 | Fork | Checks Companion pair-verify M4 before enabling encryption, ends URL playback when the control socket closes, and limits FCUP to an active control stream. Adds an AirPlay PIN-to-Companion connection test and preserves upstream's tracked `.coveragerc` when vendoring. |
| 0018 | Fork | Runs PR 2948's three HTTP fetch tests with an aiohttp loopback server. This removes a macOS reverse-DNS timeout in the Werkzeug test fixture. Checks real gzip decoding, error status and the response size limit. |
| 0019 | Fork | Logs HTTP status for URL playback control exchanges, queue commands and failed media-origin fetches. Omits headers, bodies and media URLs from these status messages. |
| 0020 | Fork | Updates the runtime, test and documentation requirements. Adapts HAP and legacy AirPlay to srptools 2 while preserving the pairing wire format. Updates zeroconf unicast requests and the test fixtures for current zeroconf and pytest-asyncio. |

## Using the fork

Set the controller identity before connecting. Use a persistent storage implementation or restore these values from the bridge's identity store at startup. `rp_id` is the Companion identifier, normally 12 hex digits. `device_id` is the controller's MAC-form public ID. These are separate from the HAP pairing identifiers.

Pass a scanned `config` with the receiver's saved credentials, a pyatv storage instance and the persistent public identifiers to this helper.

```python
import asyncio
import pyatv

async def connect_receiver(config, storage, rp_id, device_id):
    settings = await storage.get_settings(config)
    settings.info.rp_id = rp_id
    settings.info.mac = settings.info.device_id = device_id
    settings.info.name = "Agent Bridge"
    settings.protocols.companion.keepalive_interval = 15.0
    return await pyatv.connect(config, asyncio.get_running_loop(), storage=storage)
```

The interval is in seconds. Zero disables the NoOp timer. TCP keepalive stays enabled where the platform supports it. NoOp frames carry a zero-length plaintext payload and consume no encryption nonce. The heartbeat does not wait for an acknowledgement. Two consecutive failed writes or two intervals of transport backpressure abort the socket and deliver `connection_lost`. A successful heartbeat write resets the count. Detecting a silent network outage also depends on the OS TCP stack. [Companion session reference](https://github.com/basmilius/apple-protocols/blob/3431c380baf37bf41b7369418b0e1e82c907cf5f/packages/companion-link/src/session.ts)

After connecting, import `CompanionExtras` from `pyatv.protocols.companion.extras`. `CompanionExtras(atv)` uses the existing authenticated Companion socket. Fetch methods return the raw reply, including its `_c` payload. Rejected requests raise `ProtocolError`. An absent Companion service raises `NotSupportedError`. A closed Companion connection raises `ConnectionLostError`. All extras are **experimental on build 24K122**. Their message shapes were checked against the TypeScript implementation and exercised on the fake device. [Request shapes](https://github.com/basmilius/apple-protocols/blob/3431c380baf37bf41b7369418b0e1e82c907cf5f/packages/companion-link/src/messages.ts) · [Command IDs](https://github.com/basmilius/apple-protocols/blob/3431c380baf37bf41b7369418b0e1e82c907cf5f/packages/companion-link/src/const.ts)

| Method | Wire request |
| --- | --- |
| `fetch_current_now_playing_info()` | `FetchCurrentNowPlayingInfoEvent` |
| `fetch_supported_actions()` | `FetchSupportedActionsEvent` |
| `fetch_up_next_info(pagination_token=None)` | `FetchUpNextInfoEvent` with optional `PaginationTokenKey` |
| `fetch_current_top_shelf_items()` | `FetchCurrentTopShelfItemsEvent` with top-level `_btHP=false` |
| `toggle_captions()` | `ToggleCaptions` |
| `fast_forward_begin()`, `fast_forward_end()`, `rewind_begin()`, `rewind_end()` | `_mcc` 8, 9, 10 and 11. Begin requires the advertised player capability. End permits release after capabilities change. |
| `siri_press()` | Siri HID button 10, down then up |
| `subscribe(event, callback)` | Context-managed `_interest` registration. Callbacks receive event content. Overlapping subscriptions share one wire registration. |

```python
from pyatv.protocols.companion.extras import CompanionExtras

extras = CompanionExtras(atv)
async with extras.subscribe("TVSystemStatus", on_status):
    reply = await extras.fetch_current_now_playing_info()
```

#### Captions on a live player

On 2026-11-25, the living room Apple TV 4K (2nd generation, tvOS 27.2) played an AirPlay MP4 with a selectable English subtitle track. The player showed **On (Mute)**. It acknowledged `ToggleCaptions` twice. The selected track, the caption setting and the visible subtitle stayed the same.

Companion `GetCaptionSettings` (12) returned `{"_mcs": 3}`. With `SetCaptionSettings` (13), the TV accepted integer `_mcs` values 1, 2 and 3 and returned each value on the next read. It rejected 0 and rejected the Boolean `_cse` form. MediaRemote reported an automatic subtitle selection after the numeric writes. The subtitle remained visible with both English and unspecified audio language tags. Mode readback alone cannot verify subtitle visibility in this player.

The SDK's `tv_remote` could open the AirPlay player's **Subtitles** menu and select **Off** or **On**. Screenshots during playback confirmed that the English cue disappeared and reappeared. This experiment covered the AirPlay player only.

### Credential interchange

`pyatv.auth.convert` operates on credential strings in memory. In the example, `airplay` is the saved pyatv credential string, `tv_mac` is the receiver's advertised device ID, and `address` is its AirPlay `host:port`. All four HAP fields survive conversion, including the original case of the UTF-8 pairing identifiers. AirPlay and Companion use the same pyatv ordering of receiver public key, controller private seed, receiver ID and controller ID. The receiver must accept this controller identity during Companion pair-verify. Hardware may require a separate Companion PIN pairing.

```python
from pyatv.auth import convert
from pyatv.const import Protocol

config.set_credentials(Protocol.Companion, convert.companion_credentials(airplay))
text = convert.to_tvpipe(airplay, name=receiver_name, device_id=tv_mac, addr=address)
restored = convert.from_tvpipe(text)  # Pass receiver_id=bytes for a multi-receiver store.
document = convert.to_doubletake(restored, device_id=tv_mac)
restored = convert.from_doubletake(document, device_id=tv_mac)
```

Each export creates a one-receiver document. The pyatv string carries only pairing material. tvpipe names, addresses and doubletake restore tokens belong to their respective stores. The helper rejects raw and incomplete pairings. tvpipe encodes keys and receiver IDs as hex. doubletake uses a device-ID map, base64 for Go byte slices, and text for both pairing identifiers. The converter derives and checks doubletake's controller public key. [tvpipe format](https://github.com/mitsuhiko/tvpipe/blob/ba008de63d77240794119cd41ba64845e8dc01c9/crates/tvpipe/src/airplay/credentials.rs) · [doubletake format](https://github.com/omarroth/doubletake/blob/be27776beb05beaa149a7096e4722f5cba60eb37/internal/airplay/credentials.go)

### Playback limits

AirPlay 2 URL playback negotiates receiver-side PTP when the service advertises feature 41. SETUP supplies the local control address in the peer list. The receiver loads the media, so type 130 carries control commands with no sender audio or video timestamps. RAOP audio keeps its NTP timing path. The PTP negotiation and FCUP reply shape have protocol tests. Those tests cannot establish playback duration, sleep recovery or PIN display on a particular TV. Use the hardware lab for receiver-specific trials. [Lab procedure](../spikes/live/README.md)

Upstream reports FCUP success for HLS playlists on another tvOS build and a timeout when the receiver needs an entire MP4 through FCUP. [PTP peer negotiation](https://github.com/mitsuhiko/tvpipe/blob/ba008de63d77240794119cd41ba64845e8dc01c9/docs/airplay-mirroring.md) · [Upstream FCUP hardware report](https://github.com/postlund/pyatv/pull/2948)

## Rebase and verify

From the bridge root, replay in a disposable checkout. For a new release, change the tag first. If a patch conflicts, inspect its PR and both versions of the affected code. Recreate that numbered diff against the index left by the preceding patch, then stage the resolution before advancing.

```sh
bridge=$(pwd)
work=$(mktemp -d)
git clone --branch v0.18.0 --depth 1 https://github.com/postlund/pyatv "$work/pyatv"
cd "$work/pyatv"
for patch in "$bridge"/vendor/pyatv-patches/*.patch; do
    git apply --index "$patch" || exit 1
done
git diff --cached --check
git write-tree
```

The verified Git tree ID for this series is `fdda9bd11ffb4539a8d871f833696334f42ab2fc`. This is the replayed upstream index. The repository also tracks a bridge-generated `vendor/pyatv/uv.lock` that is absent from the replay. All other tracked paths and modes matched during review. To export the replay, create an empty directory and run `git archive "$(git write-tree)" | tar -x -C "$export_dir"` with `export_dir` set to that directory. Include upstream deletions and executable modes when replacing the vendored copy.

```sh
cd "$bridge"
uv venv --python 3.13 "$work/venv"
uv pip install --python "$work/venv/bin/python" \
    -r vendor/pyatv/requirements/requirements.txt \
    -r vendor/pyatv/requirements/requirements_test.txt vendor/pyatv
cd vendor/pyatv
PATH="$work/venv/bin:$PATH" "$work/venv/bin/python" -m pytest -n 4 -q -rs --timeout=30 tests
```

## Recorded test results

**1,338 passed, 6 skipped, 0 failed.** The full suite ran on macOS with CPython 3.13.17 and pytest 9.2.0. The new SRP regression test checks the public key, proof and shared key against wire values from srptools 1.0.1. The installed distribution and imported library both reported `0.18.0+atvbridge.2`.

The six upstream skips on this host are listed below. The fork adds no skips.

| Test | Cases | Upstream reason |
| --- | ---: | --- |
| `test_stream_complete_file_verify_padding` | 1 | Marked unstable pending investigation. |
| `test_stream_retransmission` | 3 | Retransmission and packet-drop cases marked unstable. |
| `test_stream_metadata_from_http` | 1 | Unconditional skip with a TODO pointing to the metadata implementation. |
| `test_keepalive_missing_sockopt[TCP_KEEPIDLE]` | 1 | Darwin uses a hard-coded equivalent for this socket option. |
