import asyncio
import hashlib
import json
import logging
import os
import re
import struct
import tempfile
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import Error, async_playwright

from ..live import page_failure
from ..page_network import PageNetworkPolicy
from ..page_proxy import PageProxy
from ..processes import child_env, reap
from ..state import Fault
from . import browsers
from .sender import STAGE_URL, Sender


_LOG = logging.getLogger(__name__)
_LEVELS = {"error": logging.ERROR, "warn": logging.WARNING, "info": logging.INFO, "debug": logging.DEBUG}
# tvpipe's debug and trace lines worth keeping: the capture page's encoder
# report (a trace), the encoded rate at tvpipe, AirPlay send timing, the
# receiver's own reports of lost and late packets, and the viewport fit.
# The others are dropped. Each pattern must match at the start of the line,
# so page console text that quotes one is dropped too.
_KEEP = re.compile(
    r"share: (?:page stats|[0-9.]+ fps from the page)|airplay: (?:timing:|receiver report)|browser: viewport"
)
_STATS_WINDOW_MS = 5000
_RECEIVER_COUNTS = ("pushed", "dropped", "dropped_full", "dropped_waiting")
# Group 1 is the hostname. The optional group skips userinfo up to its last "@".
_URL = re.compile(r"\b(?:https?|wss?)://(?:[^/\s?#]*@)?(\[[^\]/\s?#]*\]|[^/\s?#:]*)\S*")
# Bytes of a skipped oversized machine line that the warning shows.
_HEAD_BYTES = 200

# crates/tvpipe/src/browser/capture.js at the release pinned by install-tvpipe.sh.
CAPTURE_JS_LENGTH = 38420
CAPTURE_JS_SHA256 = "485907e2d6bb5b9e91aa1bd136b11908b83d72fd6ffcd0863a027f621be3487c"


def _listener_inode(pid, port):
    """Return the child-owned listening inode for the exact IPv4 loopback endpoint."""
    root = Path("/proc") / str(pid)
    endpoint = f"0100007F:{port:04X}"
    try:
        for row in (root / "net/tcp").read_text().splitlines()[1:]:
            fields = row.split()
            if fields[1] != endpoint or fields[3] != "0A":
                continue
            owner = f"socket:[{fields[9]}]"
            for fd in (root / "fd").iterdir():
                try:
                    if os.readlink(fd) == owner:
                        return owner
                except FileNotFoundError:
                    continue
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        pass
    return None


def log_event(event):
    """Write one tvpipe `log` event to the app log at its own level."""
    level, message = event.get("level"), str(event.get("message", ""))
    if level in ("debug", "trace"):
        if not _KEEP.match(message):
            return
        level = "debug"
    # A shown page's URL can carry credentials in its userinfo, path or query. Keep only the hostname.
    message = _URL.sub(r"\1", message)
    _LOG.log(_LEVELS.get(level, logging.INFO), "tvpipe: %s", message)


class MachineLines:
    """Read tvpipe's newline-framed machine events from its stdout.

    A line longer than the stream limit is skipped, and a warning shows its first bytes.
    A read that a timeout cancels keeps the skip state, so the next read resumes it.
    """

    def __init__(self, stream):
        self.stream = stream
        self.head = None  # The first bytes of the oversized line being skipped.
        self.size = 0

    async def readline(self):
        """Return the next whole line, or the partial last line at EOF."""
        while True:
            try:
                line, end = await self.stream.readuntil(b"\n"), False
            except asyncio.IncompleteReadError as exc:
                line, end = exc.partial, True
            except asyncio.LimitOverrunError as exc:
                # These bytes are buffered already, so this read does not wait.
                chunk = await self.stream.readexactly(exc.consumed)
                if self.head is None:
                    self.head, self.size = chunk[:_HEAD_BYTES], 0
                self.size += len(chunk)
                continue
            if self.head is None:
                return line
            # This line ends the oversized one.
            self.size += len(line)
            text = self.head.decode(errors="replace")
            # A URL cut at the end can have lost its "@", so it is dropped whole.
            head = _URL.sub(lambda url: "" if url.end() == len(text) else url[1], text)
            _LOG.warning("tvpipe: skipped a %d-byte machine line that starts with %s", self.size, head)
            self.head = None
            if end:
                return b""


class SentRate:
    """Log tvpipe's sent frame rate from its one-second `stats` events."""

    def __init__(self):
        self.mark = self.last = None
        self.steps = []

    def add(self, event):
        rows = event.get("receivers", [])
        now = (
            event.get("elapsed_ms", 0),
            event.get("video_frames", 0),
            *(sum(row.get(key, 0) for row in rows) for key in _RECEIVER_COUNTS),
        )
        if self.mark is None or now[0] < self.mark[0]:
            self.mark = self.last = now
            self.steps = []
            return
        # Video frames in each one-second report show the sent cadence.
        # Receiver packets count audio frames as well.
        self.steps.append(str(now[1] - self.last[1]))
        self.last = now
        span = (now[0] - self.mark[0]) / 1000
        if span * 1000 >= _STATS_WINDOW_MS:
            _LOG.debug(
                "tvpipe sent %.1f video fps over %.1f s, per report %s; receiver packets %.1f/s, "
                "dropped %d, dropped_full %d, dropped_waiting %d",
                (now[1] - self.mark[1]) / span,
                span,
                " ".join(self.steps),
                (now[2] - self.mark[2]) / span,
                *(count - start for count, start in zip(now[3:], self.mark[3:])),
            )
            self.mark = now
            self.steps = []


async def stop_sender(proc):
    # A machine-mode stop sends AirPlay TEARDOWN before Chromium exits.
    payload = b'{"type":"stop"}'
    record = struct.pack("<BQI", 6, 0, len(payload)) + payload
    try:
        async with asyncio.timeout(4):
            if proc.returncode is None:
                await proc.communicate(record)
    except (TimeoutError, BrokenPipeError, ConnectionResetError):
        pass
    finally:
        await reap(proc, group=True, sweep=browsers.sweep)


class TvpipeSender(Sender):
    def __init__(
        self,
        host,
        state,
        executable="tvpipe",
        restart_delay=1,
        ready=None,
        *,
        size="1920x1080",
        fps=10,
        fps_muted=0,
        resolve_endpoint=None,
        stage=None,
    ):
        super().__init__("mirror")
        self.size = size
        self.fps = fps
        # A muted mirror has CPU to spare for a higher rate. It never runs slower than fps.
        self.fps_muted = fps_muted
        self.stage = stage
        self.proxy = None
        self.stage_ready = ready
        self.stage_url = STAGE_URL
        self.host = host
        self.resolve_endpoint = resolve_endpoint
        self.state = state
        self.executable = executable
        self.restart_delay = restart_delay
        self.last_teardown = None
        self.url = None
        self.debug_port = None
        self.audio = "play"
        self.running_audio = None
        self.frames = 0
        self.profile = self.driver = self.page = None
        self.content_frame = self.frame_revision = None
        self.navigation_failures = {}
        self.runner = self.proc = None
        self.child_lock = asyncio.Lock()
        self.page_lock = asyncio.Lock()
        self.changed = asyncio.Event()
        self.started = False
        self.problem = None
        # Returns "menu" or "home" when the remote pressed that button in the last few seconds, else None.
        # PublicTools connects it to remote input.
        self.exit_press = lambda: None

    async def start(self, stage_url):
        if self.runner and self.running_audio != self.audio:
            await self.stop()
        async with self.child_lock:
            await browsers.sweep(
                self.proc.pid if self.proc and self.proc.returncode is None else None
            )
        if not self.runner or self.runner.done():
            browsers.log_memory("before mirror")
            self.url, self.running_audio = stage_url, self.audio
            if self.profile is None:
                self.profile = tempfile.TemporaryDirectory(prefix="atv-mirror-")
            self.problem = None
            self.changed.clear()
            self.runner = asyncio.create_task(self._run())
        try:
            async with asyncio.timeout(20):
                # The run ends without a problem when the TV ends the session.
                while not self.started and not self.problem and not self.runner.done():
                    self.changed.clear()
                    await self.changed.wait()
        except TimeoutError as exc:
            raise Fault(
                "sender_failed",
                "The mirror did not start.",
                "Check the receiver and AirPlay access.",
                True,
            ) from exc
        if self.problem:
            raise self.problem
        if not self.started:
            raise Fault("presentation_ended", "The TV ended the mirror.", "Show the page again.")
        if self.url != stage_url:
            page = await self.browser_page()
            try:
                response = await page.goto(stage_url, wait_until="domcontentloaded", timeout=8000)
                if response and response.status >= 500:
                    raise ValueError(f"HTTP {response.status}")
            except Exception as exc:
                denial = self.blocked_navigation(page.main_frame)
                if not denial and self.proxy:
                    denial = self.proxy.denial_for(stage_url)
                raise denial or page_failure(urlsplit(stage_url).hostname, exc) from exc
            self.url = stage_url
        if stage_url == self.stage_url and self.stage_ready is not None:
            await asyncio.wait_for(self.stage_ready.wait(), 8)
            # Pin the captured stage while this show owns it, including inline-only shows.
            await self.browser_page()
        self.transition("showing", ["tvpipe started", "Page loaded"])

    async def browser_page(self):
        # Tool calls and remote key dispatch can ask at the same time. Connect once.
        async with self.page_lock:
            return await self._browser_page()

    async def _browser_page(self):
        if self.page is None and self.profile:
            port, path = (Path(self.profile.name) / "DevToolsActivePort").read_text().splitlines()[:2]
            if not self.driver:
                self.driver = await async_playwright().start()
            try:
                browser = await self.driver.chromium.connect_over_cdp(f"ws://127.0.0.1:{int(port)}{path}")
            except Error as exc:
                # Browser connection errors may contain the private debugging endpoint.
                raise Fault("page_unreachable", "The captured tab is unavailable.", "Show the page again.") from exc
            try:
                tabs = [page for context in browser.contexts for page in context.pages
                        if urlsplit(page.url).scheme in ("http", "https") and await page.opener() is None]
                matches = [page for page in tabs if page.url == self.url]
                # A fresh capture may already have followed a redirect or normalized its URL.
                candidates = matches or tabs
                if len(candidates) != 1 or candidates[0].is_closed():
                    raise Fault("page_unreachable", "Cannot identify the captured tab.", "Show the page again with tv_show.")
                page = candidates[0]
                session = await page.context.new_cdp_session(page)
                try:
                    await session.send("Browser.setDownloadBehavior", {"behavior": "deny"})
                finally:
                    await session.detach()
                self.debug_port = int(port)
                page.on("response", self.record_document)
                page.on("requestfailed", self.record_failed_navigation)
                page.on("framedetached", lambda frame: self.navigation_failures.pop(frame, None))
                self.page = page
            finally:
                if self.page is None:
                    await browser.close()
        if self.page is None or self.page.is_closed():
            raise Fault("page_unreachable", "The captured tab is unavailable.", "Show the page again.")
        return self.page

    def record_document(self, response):
        if response.request.is_navigation_request():
            self.navigation_failures.pop(response.frame, None)

    def record_failed_navigation(self, request):
        if request.is_navigation_request() and self.proxy:
            denial = self.proxy.denial_for(request.url)
            if denial:
                self.navigation_failures[request.frame] = denial
            else:
                self.navigation_failures.pop(request.frame, None)

    def blocked_navigation(self, frame):
        return self.navigation_failures.get(frame)

    async def presented_frame(self, page_id, revision, direct):
        page = await self.browser_page()
        if direct:
            return page.main_frame
        if page.url != self.stage_url:
            return None
        if (revision == self.frame_revision and self.content_frame is not None
                and self.content_frame.page is page and not self.content_frame.is_detached()):
            return self.content_frame
        # Both attributes are generated by Stage.show, never supplied as selectors.
        owners = await page.locator(
            f'iframe.visible[data-page-id="{page_id}"][data-revision="{revision}"]'
        ).element_handles()
        try:
            self.content_frame = await owners[0].content_frame() if len(owners) == 1 else None
            self.frame_revision = revision
            return self.content_frame
        finally:
            for owner in owners:
                await owner.dispose()

    async def verify_document(self, url, page_id, direct, revision):
        """Inspect the committed document in the exact iframe captured by tvpipe."""
        host = urlsplit(url).hostname
        try:
            async with asyncio.timeout(8):
                frame = await self.presented_frame(page_id, revision, direct)
                if not frame:
                    raise ValueError("Chromium did not attach the requested document")
                # CDP evaluates in the target frame, including cross-origin
                # frames. An error document can have the requested frame URL.
                document = await frame.evaluate("""() => ({
                    url: document.documentURI,
                    state: document.readyState,
                    navigation: document.documentURI.startsWith('chrome-error:')
                        ? performance.getEntriesByType('navigation')[0]?.name : null,
                    error: document.querySelector('.error-code, #error-code')?.textContent
                })""")
                if (
                    urlsplit(document["url"]).scheme not in ("http", "https")
                    or document["state"] == "loading"
                ):
                    denial = self.blocked_navigation(frame)
                    if not denial and self.proxy:
                        # A fresh stage iframe can fail before CDP attaches.
                        # Its navigation entry retains the last redirect URL.
                        denial = self.proxy.denial_for(document.get("navigation") or frame.url)
                    if denial:
                        raise denial
                    raise ValueError(
                        document["error"]
                        or f"Chromium loaded {document['url']} ({document['state']})"
                    )
        except Fault:
            raise
        except Exception as exc:
            raise page_failure(host, exc) from exc

    async def confirm_frame(self, timeout=3):
        initial = self.frames
        try:
            async with asyncio.timeout(timeout):
                while self.frames <= initial:
                    await asyncio.sleep(0.05)
            return True
        except TimeoutError:
            return False

    async def stop(self):
        try:
            if self.runner:
                self.runner.cancel()
                with suppress(asyncio.CancelledError):
                    await self.runner
        finally:
            self.runner = None
            try:
                if self.driver:
                    async with asyncio.timeout(3):
                        await self.driver.stop()
            finally:
                self.page = self.driver = None
                self.started, self.problem, self.frames = False, None, 0
                try:
                    if self.profile:
                        self.profile.cleanup()
                        self.profile = None
                finally:
                    await browsers.sweep()
                    browsers.log_memory("after mirror")
        self.transition("idle")

    async def _run(self):
        delay = self.restart_delay
        failed_host = None
        while True:
            proc = None
            ended = None  # Evidence when the TV ended the session. Then the mirror does not restart.
            self.frames = 0
            self.transition("starting")
            try:
                host, port = (
                    await self.resolve_endpoint(failed_host) if self.resolve_endpoint else (self.host, 7000)
                )
                self.state.write_tvpipe(host, port)
                async with self.child_lock:
                    await browsers.sweep()
                    policy = self.stage.page_network if self.stage else PageNetworkPolicy()
                    self.proxy = PageProxy(policy, open_capture=self.open_capture)
                    proxy_url = await self.proxy.start()
                    self.proc = proc = await browsers.spawn(
                        self.executable,
                        *(["--debug"] if _LOG.isEnabledFor(logging.DEBUG) else []),
                        "--machine",
                        "share",
                        self.url,
                        "--to",
                        f"{host}:{port}",
                        "--size",
                        self.size,
                        "--fps",
                        str(max(self.fps, self.fps_muted) if self.audio == "mute" else self.fps),
                        "--bitrate",
                        "2500" if self.size == "1280x720" else "4000",
                        *(["--profile", self.profile.name] if self.profile else []),
                        *(["--no-audio"] if self.audio == "mute" else []),
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        env=child_env(
                            TVPIPE_CONFIG_DIR=str(self.state.root / "tvpipe"),
                            TVPIPE_BROWSER=browsers.CHROMIUM_WRAPPER,
                            ATV_PAGE_PROXY=proxy_url,
                        ),
                    )
                proc.stdin.write(b"SLRW")
                await proc.stdin.drain()
                lines = MachineLines(proc.stdout)
                rate = SentRate()
                while True:
                    try:
                        line = await asyncio.wait_for(lines.readline(), 0.5)
                    except TimeoutError:
                        if proc.returncode is not None:
                            break
                        continue
                    if not line:
                        break
                    event = json.loads(line)
                    kind = event.get("type")
                    if kind == "pin-required" or event.get("code") in (
                        "pairing-required",
                        "pin-missing",
                    ):
                        raise Fault(
                            "pairing_required",
                            "tvpipe requested AirPlay pairing.",
                            "Pair AirPlay and show the page again.",
                        )
                    if kind == "log":
                        log_event(event)
                    if kind in ("capture", "started"):
                        _LOG.debug("tvpipe %s: %s", kind, json.dumps(event))
                    if kind == "stats":
                        self.frames = sum(
                            row.get("pushed", 0) for row in event.get("receivers", [])
                        )
                        rate.add(event)
                    if kind == "started":
                        self.transition("showing", ["tvpipe started"])
                        failed_host = None
                        self.started = True
                        self.changed.set()
                        delay = self.restart_delay
                    if kind == "error" and not self.started:
                        raise Fault(
                            "sender_failed",
                            f"tvpipe startup failed ({event.get('code') or event.get('kind')}).",
                            "Check the receiver address, AirPlay access and Chromium.",
                            True,
                        )
                    # tvpipe reports "ended" when the TV closes the video connection, as Menu or Home does.
                    # A network loss cannot close it. The TV can close the control connection first.
                    # tvpipe calls that "lost" and reconnects, so a Menu or Home press just before decides.
                    if kind == "receiver" and self.started:
                        if event.get("state") == "ended":
                            ended = ["tvpipe reported that the TV ended the session"]
                        elif event.get("state") == "lost" and (press := self.exit_press()):
                            ended = [f"Remote {press} pressed, then tvpipe lost the TV"]
                    if kind == "end" and event.get("reason") == "receivers-gone" and self.started:
                        receivers = event.get("receivers") or []
                        if receivers and all(row.get("end") == "ended" for row in receivers):
                            ended = ["tvpipe reported that the TV ended the session"]
                    if ended:
                        # Report it before tvpipe's cleanup, which can take seconds.
                        _LOG.info("The TV ended the mirror (%s). The mirror does not restart.", ended[0])
                        self.transition("ended", ended)
                        self.changed.set()
                        break
                    if kind == "end" and event.get("reason") != "error":
                        break
                if not self.started:
                    raise Fault(
                        "sender_failed",
                        "tvpipe ended before starting.",
                        "Check AirPlay access.",
                        True,
                    )
            except Exception as exc:  # noqa: BLE001 - Report child process and machine protocol errors to the waiting caller.
                if (
                    self.resolve_endpoint
                    and proc
                    and failed_host is None
                    and isinstance(exc, Fault)
                    and exc.code == "sender_failed"
                ):
                    failed_host = host
                    continue
                self.problem = (
                    exc
                    if isinstance(exc, Fault)
                    else Fault(
                        "sender_failed",
                        "Cannot start or read tvpipe.",
                        "Check the app installation.",
                        True,
                    )
                )
                self.fail(self.problem)
                self.changed.set()
                return
            finally:
                self.started = False
                self.page = self.debug_port = None
                self.content_frame = self.frame_revision = None
                self.navigation_failures.clear()
                cleanup = asyncio.create_task(self._clean_child(proc))
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await cleanup
                    raise
            if ended:
                return
            failed_host = host
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def open_capture(self, host, port):
        """Identify tvpipe's capture WebSocket on this connection before sending page bytes."""
        proc = self.proc
        if host != "127.0.0.1" or proc is None or proc.returncode is not None:
            return None
        inode = await asyncio.to_thread(_listener_inode, proc.pid, port)
        if not inode:
            return None
        writer = None
        try:
            async with asyncio.timeout(2):
                reader, writer = await asyncio.open_connection(host, port)
                if (
                    proc is not self.proc
                    or proc.returncode is not None
                    or inode != await asyncio.to_thread(_listener_inode, proc.pid, port)
                ):
                    return None
                # tvpipe v0.2.0 does not announce the capture port in URL share mode.
                # Its other listeners (including a browser-player sink) must stay private.
                # Fingerprint the pinned release's public script on the very socket
                # that will carry the authenticated upgrade. No second dial is made.
                writer.write(f"GET /capture.js HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
                await writer.drain()
                headers = await reader.readuntil(b"\r\n\r\n")
                length = re.search(rb"(?im)^Content-Length: ([0-9]+)\r$", headers)
                if not headers.startswith(b"HTTP/1.1 200 ") or not length:
                    return None
                if length[1] != str(CAPTURE_JS_LENGTH).encode():
                    return None
                capture = await reader.readexactly(CAPTURE_JS_LENGTH)
                if hashlib.sha256(capture).hexdigest() != CAPTURE_JS_SHA256:
                    return None
                streams = reader, writer
                writer = None  # Hand ownership of this verified connection to the proxy.
                return streams
        except (OSError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            return None
        finally:
            if writer:
                writer.close()
                with suppress(OSError):
                    await writer.wait_closed()

    async def wait_for_airplay_handoff(self):
        """Give the receiver a second after the last mirror TEARDOWN."""
        if self.last_teardown is not None:
            remaining = self.last_teardown + 1 - asyncio.get_running_loop().time()
            if remaining > 0:
                await asyncio.sleep(remaining)

    async def _clean_child(self, proc):
        async with self.child_lock:
            try:
                if proc:
                    await stop_sender(proc)
                    self.last_teardown = asyncio.get_running_loop().time()
            finally:
                self.proc = None
                try:
                    if self.driver:
                        async with asyncio.timeout(3):
                            await self.driver.stop()
                finally:
                    self.driver = None
                    if self.proxy:
                        await self.proxy.close()
                        self.proxy = None
                    await browsers.sweep()
