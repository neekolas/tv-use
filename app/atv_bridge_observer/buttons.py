# SPDX-License-Identifier: GPL-3.0-or-later
"""Watch remote presses in the receiver's log stream and report them as events."""

import asyncio
import logging
import os
import plistlib
import re
import time
from contextlib import suppress

from pymobiledevice3.services.os_trace import OsActivityStreamFlag, OsTraceService

from . import touch

# ---------------------------------------------------------------------------
# Press matcher. Change only this block when a remote logs a different line.
# The match uses the process, the log category and the HID page and usage.
# It must not depend on where the press came from.
# Swipes on a touch surface are matched in touch.py, which belongs to this block.
# tv_remote presses: generic desktop page 0x1, verified on tvOS 26.6.1.
# Physical clickpad Siri Remote: consumer page 0xC, verified on tvOS 27.2.
PROCESS = "backboardd"
CATEGORY = "Button"
PRESS = re.compile(r"\bpage:0x([0-9A-Fa-f]+) usage:0x([0-9A-Fa-f]+) downEvent:([01])\b")
# Other lines that the same press logs. They carry no new information.
FOLLOW_UP = re.compile(r"\b(began|finished): ")
USAGE_KEYS = {
    (0x01, 0x8B): "left",
    (0x01, 0x8A): "right",
    (0x01, 0x8C): "up",
    (0x01, 0x8D): "down",
    (0x0C, 0x42): "up",
    (0x0C, 0x43): "down",
    (0x0C, 0x44): "left",
    (0x0C, 0x45): "right",
    (0x0C, 0x80): "select",
    # tvOS handles these two and ends the mirror. The app reports them but never forwards them.
    # Menu: "Menu page:0x1 usage:0x86", physical Siri Remotes on tvOS 26.6.1 and 27.2.
    # TV (Home): "TV page:0xC usage:0x60", physical clickpad Siri Remote on tvOS 27.2.
    (0x01, 0x86): "menu",
    (0x0C, 0x60): "home",
}


def match_press(process, category, message):
    """Return (key, known). key names a key-down press. known is False for a line worth a diagnostic log."""
    if process != PROCESS or category != CATEGORY:
        return None, True
    found = PRESS.search(message or "")
    if not found:
        return None, bool(FOLLOW_UP.search(message or ""))
    key = USAGE_KEYS.get((int(found[1], 16), int(found[2], 16)))
    if found[3] != "1":
        return None, True
    return key, key is not None


# ---------------------------------------------------------------------------

# Button lines are NOTICE level and need only PAYLOAD. Touch lines are DEBUG level.
# Never add HISTORICAL: it replays old log lines as new presses.
# Volume of backboardd lines in a full-debug capture: under 1 line/s when idle, about
# 65 lines/s while a finger is on the remote, and one burst of 513 lines when the remote woke.
STREAM_FLAGS = int(OsActivityStreamFlag.PAYLOAD | OsActivityStreamFlag.DEBUG)
STALE_S = 2.0
CONNECT_GRACE_S = 1.0
# A late line that stays late by the same amount for this long is a clock step, not a backlog.
# Accepted limit: a backlog replayed at near real-time pace for longer than this would pass.
# No trigger is known, because the stream never asks for HISTORICAL.
REBASE_AFTER_S = 3.0
REBASE_TOLERANCE_S = 0.5
OPEN_TIMEOUT_S = 8
ESTABLISH_TIMEOUT_S = 10
PROBE_INTERVAL_S = 30
PROBE_RETRY_S = 5
RETRY_MIN_S = 0.5
RETRY_MAX_S = 30
UNMATCHED_LIMIT = 6  # diagnostic lines of each event per UNMATCHED_WINDOW_S
UNMATCHED_WINDOW_S = 10
ONCE_LIMIT = 12  # "first line" diagnostics per watch start
LINE_LIMIT = 600  # characters of a log line kept in a diagnostic


class Freshness:
    """Decide whether a log line is live. Old lines must never become key presses.

    now is the host's monotonic clock, so a host clock change has no effect.
    device is the TV's wall clock. Only differences between lines are used.
    """

    def __init__(self, now, lag=float("inf")):
        self.connected = now
        self.latest = float("-inf")
        # The baseline of an earlier connection still holds. With it, replayed lines
        # are late from the first one. Without it, only the connect grace period protects.
        self.lag = lag
        self.late = None  # (first arrival, lag) of a run of late lines with the same lag

    def stale(self, device, now):
        # The smallest lag seen is the clock offset plus the normal delay.
        lag = now - device
        late = lag - self.lag > STALE_S or self.latest - device > STALE_S
        if late:
            # A backlog arrives in a burst and its lag shrinks. After a clock step on the TV
            # every line is late by the same amount. Accept that as the new baseline.
            if self.late and abs(lag - self.late[1]) <= REBASE_TOLERANCE_S:
                if now - self.late[0] >= REBASE_AFTER_S:
                    self.lag, self.latest, self.late = lag, device, None
                    return now - self.connected < CONNECT_GRACE_S
            else:
                self.late = (now, lag)
            return True
        self.late = None
        self.lag = min(self.lag, lag)
        self.latest = max(self.latest, device)
        return now - self.connected < CONNECT_GRACE_S


class Unavailable(Exception):
    """The watch cannot run now. The message is a stable reason code reported to the app.

    Codes: screen_not_paired, pairing_in_progress, screen_pairing_invalid, tv_unreachable,
    service_missing, process_not_found, process_restarted, stream_failed.
    """


def mark_established(connection, established):
    """Set the event when the device has accepted the stream.

    syslog() in pymobiledevice3 11.61 reads the answer to StartActivity in three steps:
    a 4-byte count, the length, then the status plist. Only the third read tells
    whether the device accepted. An accepted idle stream then stays silent.
    """
    recvall = connection.recvall
    reads = 0

    async def watched(size):
        nonlocal reads
        data = await recvall(size)
        reads += 1
        if reads == 3:
            with suppress(Exception):
                if plistlib.loads(data).get("Status") == "RequestSuccessful":
                    established.set()
        return data

    connection.recvall = watched


async def process_pid(rsd, name=PROCESS):
    async with asyncio.timeout(8), OsTraceService(lockdown=rsd) as service:
        payload = (await service.get_pid_list()).get("Payload", {})
    pids = [int(pid) for pid, info in payload.items() if info.get("ProcessName") == name]
    if len(pids) != 1:
        raise Unavailable("process_not_found")
    return pids[0]


class ButtonWatch:
    """Run the log stream on the observer's tunnel. Request handling never waits for it."""

    def __init__(self, observer, emit):
        self.observer = observer
        self.emit = emit
        self.task = None
        self.state = None
        self.delay = RETRY_MIN_S
        self.lag = float("inf")  # Lag baseline, kept across reconnects.
        self.unmatched = {}  # Event name to the times of its last diagnostic lines.
        self.logged = set()
        self.touch = touch.TouchRecognizer()
        self.momentum = None

    def start(self):
        if not self.task or self.task.done():
            self.state = None
            self.logged = set()
            self.task = asyncio.create_task(self._run())

    async def stop(self):
        if self.task:
            task, self.task = self.task, None
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def report(self, state, reason=None):
        if (state, reason) != self.state:
            self.state = (state, reason)
            self.emit({"event": "buttons", "state": state, **({"reason": reason} if reason else {})})

    async def _run(self):
        self.delay = RETRY_MIN_S
        while True:
            try:
                await self._stream()
            except asyncio.CancelledError:
                raise
            except Unavailable as exc:
                self.report("unavailable", str(exc))
            except Exception as exc:  # noqa: BLE001 - Any stream failure leads to a retry.
                logging.info("Button watch stopped (%s). Retrying in %.1f s.", type(exc).__name__, self.delay)
                self.report("unavailable", "stream_failed")
            await asyncio.sleep(self.delay)
            self.delay = min(self.delay * 2, RETRY_MAX_S)

    async def _stream(self):
        observer = self.observer
        # A PIN exchange closes the tunnel on purpose. Do not reopen it.
        if observer.pairing_active():
            raise Unavailable("pairing_in_progress")
        if not observer.record.is_file():
            raise Unavailable("screen_not_paired")
        try:
            # Requests share this open. A request never waits for a second connect.
            async with asyncio.timeout(OPEN_TIMEOUT_S):
                rsd = await observer.open_tunnel()
        except Exception as exc:  # noqa: BLE001 - The reason code tells the app what failed.
            invalid = type(exc).__name__ == "ScreenPairingInvalid"
            raise Unavailable("screen_pairing_invalid" if invalid else "tv_unreachable") from exc
        if OsTraceService.RSD_SERVICE_NAME not in rsd.peer_info.get("Services", {}):
            raise Unavailable("service_missing")
        pid = await process_pid(rsd)
        async with OsTraceService(lockdown=rsd) as service:
            established = asyncio.Event()
            mark_established(service.service, established)
            fresh = Freshness(time.monotonic(), self.lag)
            reader = asyncio.create_task(self._read(service.syslog(pid=pid, stream_flags=STREAM_FLAGS), fresh))
            accepted = asyncio.create_task(established.wait())
            try:
                # Report "active" only after the device accepted the stream.
                done, _ = await asyncio.wait({reader, accepted}, timeout=ESTABLISH_TIMEOUT_S, return_when=asyncio.FIRST_COMPLETED)
                if reader in done:
                    reader.result()
                if not established.is_set():
                    raise ConnectionError("The device did not accept the log stream")
                logging.info("Button watch active (pid %d, flags %#x)", pid, STREAM_FLAGS)
                self.report("active")
                self.delay = RETRY_MIN_S
                # An idle stream is silent, which is normal.
                wait, failures = PROBE_INTERVAL_S, 0
                while True:
                    done, _ = await asyncio.wait({reader}, timeout=wait)
                    if done:
                        reader.result()
                        raise ConnectionError("The log stream ended")
                    # Liveness does not depend on log traffic. The probe also sees a new pid.
                    try:
                        current = await process_pid(rsd)
                    except Exception:  # noqa: BLE001 - Count every probe failure the same way.
                        failures += 1
                        if failures < 2:
                            wait = PROBE_RETRY_S
                            continue
                        await self._reset_tunnel(rsd)
                        raise
                    if current != pid:
                        raise Unavailable("process_restarted")
                    wait, failures = PROBE_INTERVAL_S, 0
            finally:
                self.lag = fresh.lag
                self._cancel_momentum()
                self.touch.reset()
                reader.cancel()
                accepted.cancel()
                await asyncio.gather(reader, accepted, return_exceptions=True)

    async def _reset_tunnel(self, rsd):
        # Wait for a request in flight. Its own failure handling may already have closed the tunnel.
        async with self.observer.busy:
            if self.observer.tunnel.rsd is rsd:
                logging.info("Button watch found a dead tunnel. Closing it.")
                with suppress(Exception):
                    await self.observer.close_channels()

    async def _read(self, stream, fresh):
        async for entry in stream:
            now = time.time()
            device = entry.timestamp.timestamp()
            stale = fresh.stale(device, time.monotonic())
            process = os.path.basename(entry.filename)
            category = entry.label.category if entry.label else None
            if process == PROCESS and category == touch.CATEGORY:
                self._touch(entry.message, device, now, stale)
                continue
            key, known = match_press(process, category, entry.message)
            if process == PROCESS and category == CATEGORY and "downEvent:1" in (entry.message or ""):
                # Any button, also one with no key here, ends a swipe under the finger.
                self.touch.button(device)
                self._cancel_momentum()
            if not known:
                self._unmatched(entry.message, now)
            if process == PROCESS and category != CATEGORY and touch.HINT.search(entry.message or ""):
                # A remote may log its touches in a category that touch.py does not read.
                self._once(("category", category), f"First line about touch in log category {category}", entry.message)
            if key and not stale:
                self.emit({"event": "button", "key": key, "device_time": device, "received": now})
            elif key:
                logging.info("Dropped a stale %s press", key)

    def _once(self, key, what, message):
        """Log one raw line per key and watch start. The app log then shows what a remote sends."""
        if key not in self.logged and len(self.logged) < ONCE_LIMIT:
            self.logged.add(key)
            # One line: the app reads the level of each observer log line from its start.
            logging.info("%s: %s", what, " ".join(str(message).split())[:LINE_LIMIT])

    def _touch(self, message, device, now, stale):
        # These two do not depend on the matcher in touch.py: a remote can log another line shape.
        self._once("touch", "First touch line of this watch", message)
        if "down" in (message or ""):
            self._once("down", "First touch-down line of this watch", message)
        if stale:
            # Old touch lines must not move anything, and a half-seen contact is useless.
            self.touch.reset()
            self._cancel_momentum()
            return
        gesture = self.touch.touch(message, device)
        if gesture.unmatched:
            self._unmatched(message, now, "touch_unmatched")
        for report in gesture.reports:
            self.emit({"event": "gesture", **report})
        if gesture.touch_down:
            self._cancel_momentum()
        for key in gesture.keys:
            self.emit({"event": "button", "key": key, "device_time": device, "received": now, "gesture": "swipe"})
        if gesture.momentum:
            self._cancel_momentum()
            self.momentum = asyncio.create_task(self._glide(*gesture.momentum))

    async def _glide(self, key, delays):
        """Send the arrows that follow a flick. A new touch or a click cancels the rest."""
        started = time.monotonic()
        for delay in delays:
            await asyncio.sleep(max(0, started + delay - time.monotonic()))
            self.emit({"event": "button", "key": key, "received": time.time(), "gesture": "momentum"})

    def _cancel_momentum(self):
        if self.momentum:
            self.momentum.cancel()
            self.momentum = None

    def _unmatched(self, message, now, event="button_unmatched"):
        # Each event has its own budget, so a flood of touch lines cannot hide button lines.
        recent = self.unmatched[event] = [at for at in self.unmatched.get(event, []) if now - at < UNMATCHED_WINDOW_S]
        if len(recent) < UNMATCHED_LIMIT:
            recent.append(now)
            self.emit({"event": event, "message": str(message)[:300]})
