"""Exercise page control and egress with a real, network-isolated Chromium/tvpipe."""

import asyncio
import json
import socket
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from aiohttp import web

from atv_bridge_app.page_network import PageNetworkPolicy
from atv_bridge_app.present import browsers
from atv_bridge_app.present.sender import STAGE_URL
from atv_bridge_app.present.tvpipe import TvpipeSender
from atv_bridge_app.stage import Stage
from atv_bridge_app.state import State, Fault
from atv_bridge_app.tools import Tools
from atv_bridge_app.tools.contract import FILL_RECIPE, OUTLINE_RECIPE, SELECT_RECIPE, WAIT_RECIPE
from atv_bridge_app.tools.public import PublicTools

PAGE = """<!doctype html><title>Recipe</title><h1>Live page</h1>
<section aria-label="Recipe steps"><div id="status" role="status">Step 1</div>
<button id="next">Next</button>
<input aria-label="Note"><input type="password" aria-label="Password" value="private-fixture">
<input aria-label="API token" value="private-fixture">
<input type="checkbox" checked aria-label="Ready">
<button hidden>Hidden control</button><div inert><button>Inert control</button></div>
<div aria-hidden="true"><button>Aria hidden</button></div><button style="display:none">CSS hidden</button>
</section><iframe srcdoc="<button>Nested secret</button>"></iframe>
<a href="/continued" id="continue">Continue</a><div style="height:300vh"></div>
<script nonce="fixture">window.setStep = step => document.querySelector('#status').textContent = 'Step ' + step;
document.querySelector('#next').onclick = () => window.setStep(2);</script>"""

BUNDLE = {
    "index.html": PAGE + '<script src="assets/tv.js"></script><a id="detail" href="detail.html">Details</a>',
    "assets/tv.js": 'document.body.dataset.asset = "loaded";',
    "detail.html": '<title>Details</title><a href="#notes">Notes</a><section id="notes">Notes</section>',
}


def check_listener(profile, proxy_url):
    # Check the running browser, including the final flag order from tvpipe.
    commands = [p.read_bytes().split(b'\0') for p in Path('/proc').glob('[0-9]*/cmdline') if p.exists()]
    args = next(cmd for cmd in commands if ('--user-data-dir=' + profile.name).encode() in cmd
                and not any(a.startswith(b'--type=') for a in cmd))
    assert [a for a in args if a.startswith(b'--autoplay-policy=')][-1] == b'--autoplay-policy=no-user-gesture-required'
    for name, value in {"proxy-server": proxy_url, "proxy-bypass-list": "<-loopback>",
                        "webrtc-ip-handling-policy": "disable_non_proxied_udp"}.items():
        prefix = ("--" + name + "=").encode()
        values = [arg.removeprefix(prefix) for arg in args if arg.startswith(prefix)]
        assert values and values[-1] == value.encode(), (name, values)
    assert b"--disable-quic" in args
    port = int((Path(profile.name) / "DevToolsActivePort").read_text().splitlines()[0])
    addresses = []
    for table in ("tcp", "tcp6"):
        for line in Path("/proc/net", table).read_text().splitlines()[1:]:
            address, port_hex = line.split()[1].split(":")
            if int(port_hex, 16) == port and line.split()[3] == "0A":
                addresses.append(address)
    assert addresses and set(addresses) <= {"0100007F", "00000000000000000000000001000000"}, addresses


async def run(tools, script, arg=None, *, label=None, **options):
    await asyncio.sleep(.26)
    started = time.perf_counter()
    reply = await tools.call("tv_page_run", {"script": script, "arg": arg, **options})
    if label:
        print(label, "ms", round(1000 * (time.perf_counter() - started)), flush=True)
    assert reply["ok"], reply
    return reply["data"]


async def exercise_outline(tools, frame, direct):
    result = await run(tools, "return [window === top, window.setStep(arg)]", 1)
    assert result["result"] == [direct, "Step 1"]
    rows = (await run(tools, OUTLINE_RECIPE))["result"]
    assert {"role": "heading", "text": "Live page"} in rows and {"role": "button", "text": "Next"} in rows
    assert {"role": "checkbox", "text": "Ready", "value": True} in rows
    assert all(word not in str(rows) for word in ["private-fixture", "Hidden control", "Inert control",
                                                  "Aria hidden", "CSS hidden", "Nested secret"])
    await run(tools, "document.querySelector('#next').click()")
    assert (await run(tools, "document.querySelector('#status').innerText"))["result"] == "Step 2"
    await asyncio.sleep(.26)
    navigation = await tools.call("tv_page_run", {"script": "document.querySelector('#continue').click()"})
    assert navigation["ok"] or navigation["error"]["code"] == "stale_view", navigation
    await frame.wait_for_url("**/continued")
    assert (await run(tools, "return [location.pathname, document.querySelector('#status').innerText]"))["result"] == ["/continued", "Step 1"]


async def exercise_scripts(tools, frame, submissions):
    # Simulate a controlled input. A framework observes an own-property setter
    # and ignores input events when the DOM value equals its cached value.
    await frame.evaluate("""() => {
        const form = document.createElement('form');
        form.id = 'edit';
        form.action = '/submitted';
        form.innerHTML = '<input id="note" name="note"><textarea id="details"></textarea>'
           + '<select id="size" name="size"><option>s</option><option>l</option></select>'
           + '<button id="advance" type="button">Advance</button><output id="result"></output>';
        document.body.append(form);
        const el = form.querySelector('#note');
        const native = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
        let tracked = el.value;
        Object.defineProperty(el, 'value', {get: native.get, set(value) {
            tracked = String(value); native.set.call(this, value);
        }});
        form.addEventListener('input', event => {
            if (event.target === el && tracked !== el.value) form.dataset.note = tracked = el.value;
        });
        form.addEventListener('change', event => form.dataset[event.target.id + 'Change'] = event.target.value);
        form.querySelector('#advance').onclick = event => {
            form.dataset.trusted = String(event.isTrusted);
            setTimeout(() => form.querySelector('#result').textContent = 'Done', 500);
        };
        el.addEventListener('keydown', event => form.dataset.down = event.key);
        el.addEventListener('keyup', event => form.dataset.up = event.key);
    }""")
    await run(tools, FILL_RECIPE, {"selector": "#note", "text": "fixture"})
    await run(tools, FILL_RECIPE, {"selector": "#details", "text": "Extra"})
    await run(tools, SELECT_RECIPE, "l")
    await run(tools, "document.querySelector('#advance').click()")
    assert (await run(tools, WAIT_RECIPE))["result"] == "Done"
    data = await run(tools, """const form = document.querySelector('#edit');
            const el = form.querySelector('#note'); el.focus();
            for (const type of ['keydown', 'keyup'])
                document.activeElement.dispatchEvent(new KeyboardEvent(type, {key: 'Z', bubbles: true}));
            form.scrollIntoView();
            return {state: {...form.dataset}, value: el.value, scrolled: scrollY > 0};""")
    assert data["result"] == {"state": {"note": "fixture", "noteChange": "fixture", "detailsChange": "Extra",
        "sizeChange": "l", "trusted": "false", "down": "Z", "up": "Z"}, "value": "fixture", "scrolled": True}, data
    previous = len(submissions)
    await asyncio.sleep(.25)
    submitted = await tools.call("tv_page_run", {"script": "document.querySelector('form').requestSubmit()"})
    assert submitted["ok"] or submitted["error"]["code"] == "stale_view", submitted
    await frame.wait_for_url("**/submitted?*")
    assert submissions[previous:] == ["note=fixture&size=l"]
    assert (await run(tools, "document.querySelector('h1').innerText"))["result"] == "Live page"


async def exercise_large_page(tools, frame):
    await frame.evaluate("""() => {
        const buttons = Array.from({length: 1500}, (_, i) => {
            const button = document.createElement('button');
            button.textContent = 'Control ' + i;
            return button;
        });
        document.body.replaceChildren(...buttons);
    }""")
    rows = (await run(tools, OUTLINE_RECIPE, label="1500 controls first page"))["result"]
    assert len(rows) == 40 and rows[0]["text"] == "Control 0"
    find = "[...document.querySelectorAll(arg.selector)].filter(el => el.innerText.includes(arg.text)).map(el => el.innerText)"
    matches = await run(tools, find, {"selector": "button", "text": "Control 1499"}, label="1500 controls find last")
    assert matches["result"] == ["Control 1499"]

    await frame.evaluate("""() => {
        document.body.innerHTML = '<header><nav aria-label="Repository"><a href="/code">Code</a>'
          + '<a href="/issues">Issues</a><button>Watch</button></nav></header>'
          + '<main><h1>Project files</h1><table><tbody></tbody></table></main>';
        const body = document.querySelector('tbody');
        for (let i = 0; i < 145; i++) {
            const tr = body.insertRow();
            tr.innerHTML = '<td><a href="/file/' + i + '">module-' + i + '.py</a></td>'
              + '<td>Update the module documentation</td><td><button>History ' + i + '</button></td>';
        }
        const article = document.querySelector('main').appendChild(document.createElement('article'));
        for (let i = 0; i < 60; i++) {
            article.insertAdjacentHTML('beforeend', '<section aria-label="Guide ' + i + '">'
              + '<h2>Configuration ' + i + '</h2><p>See the reference for details.</p></section>');
        }
    }""")
    rows = (await run(tools, OUTLINE_RECIPE, label="repository first page"))["result"]
    assert len(rows) == 40 and {"role": "link", "text": "Issues"} in rows
    links = await run(tools, find, {"selector": "a[href]", "text": "Issues"}, label="repository find Issues")
    assert links["result"] == ["Issues"]
    assert (await run(tools, "document.querySelector('h1').innerText", label="repository targeted read"))["result"] == "Project files"
    await run(tools, "document.querySelector('nav button').click()", label="repository script click")


async def exercise_limits(tools):
    capped = await run(tools, "'🍅'.repeat(20000)")
    assert capped["truncated"] and capped["bytes"] == 80002
    assert len(capped["preview"].encode()) <= 65536 and "�" not in capped["preview"]
    await asyncio.sleep(.26)
    timeout = await tools.call("tv_page_run", {"script": "new Promise(() => {})", "timeout_ms": 150})
    assert timeout["error"]["code"] == "script_error" and "150 ms" in timeout["summary"], timeout
    tools.page_scripts = False
    try:
        for script in ("document.title", OUTLINE_RECIPE):
            reply = await tools.call("tv_page_run", {"script": script})
            assert reply["error"]["code"] == "scripts_disabled" and "page_scripts" in reply["error"]["fix"]
    finally:
        tools.page_scripts = True
    assert (await run(tools, "Promise.resolve(document.title)"))["result"] == "Recipe"


async def exercise_bundle(tools, frame):
    assert (await run(tools, "document.body.dataset.asset"))["result"] == "loaded"
    # A full document navigation can replace the script's execution context.
    await asyncio.sleep(.26)
    click = await tools.call("tv_page_run", {"script": "document.querySelector('#detail').click()"})
    assert click["ok"] or click["error"]["code"] == "stale_view", click
    await frame.wait_for_url("**/detail.html")
    assert (await run(tools, "document.title"))["result"] == "Details"
    await run(tools, "document.querySelector('a').click()")
    await frame.wait_for_url("**/detail.html#notes")
    assert (await run(tools, "document.querySelector(':target').innerText"))["result"] == "Notes"


async def exercise_egress(tools, mirror):
    urls = [
        "http://127.0.0.1:8767/ok",
        "http://127.0.0.1:8765/api/health",
        f"http://127.0.0.1:{mirror.debug_port}/json/version",
        # HTTPS avoids Chromium's earlier mixed-content check for insecure HTTP.
        "https://10.0.0.1/health",
        "https://192.168.0.1/health",
        "https://blocked.page.test:8765/health",
    ]
    # no-cors makes even an opaque HTTP error response observable as success.
    result = await run(tools, """return Promise.all(arg.map(async url => {
        try { await fetch(url, {mode: 'no-cors', cache: 'no-store'}); return 'response'; }
        catch (error) { return error.name; }
    }))""", urls)
    assert result["result"] == ["response"] + ["TypeError"] * (len(urls) - 1), result
    for url in urls[1:]:
        # With --network none a LAN request would also fail for lack of a route.
        assert mirror.proxy.denial_for(url), f"Missing proxy denial for {url}"
    events = await run(tools, """const test = (client, success) => new Promise(resolve => {
        client[success] = () => { client.close(); resolve('open'); };
        client.onerror = () => { client.close(); resolve('error'); };
    });
    return Promise.all(arg.flatMap(port => [
        test(new WebSocket('ws://127.0.0.1:' + port + '/socket'), 'onopen'),
        test(new EventSource('http://127.0.0.1:' + port + '/events'), 'onmessage'),
    ]));""", [8767, 8765])
    assert events["result"] == ["open", "open", "error", "error"], events


async def verify_capture(mirror, counts):
    # Check fresh encoded video after switching document or restarting tvpipe.
    # The JavaScript state and the total receiver records can include setup/audio.
    pid = mirror.proc.pid
    initial = counts.get(pid, 0)
    async with asyncio.timeout(10):
        while counts.get(pid, 0) <= initial:
            await asyncio.sleep(.1)
    print("tvpipe captured video frames", counts[pid] - initial, flush=True)


async def exercise_webrtc(tools):
    loop = asyncio.get_running_loop()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as stun:
        stun.bind(("127.0.0.1", 0))
        stun.setblocking(False)
        address = stun.getsockname()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
            control.sendto(b"fixture control", address)
        assert await asyncio.wait_for(loop.sock_recv(stun, 64), 1) == b"fixture control"
        gathering = await run(tools, """const pc = new RTCPeerConnection({
            iceServers: [{urls: 'stun:127.0.0.1:' + arg}]
        });
        try {
            pc.createDataChannel('canary');
            const candidates = [];
            const done = new Promise(resolve => {
                pc.onicecandidate = event => {
                    if (event.candidate) candidates.push(event.candidate.protocol);
                    else resolve();
                };
            });
            await pc.setLocalDescription(await pc.createOffer());
            // With no permitted ICE transports, Chromium can remain in gathering.
            // Observe the whole interval unless it reports completion sooner.
            if (pc.iceGatheringState !== 'complete') {
                await Promise.race([done, new Promise(resolve => setTimeout(resolve, 3000))]);
            }
            return {state: pc.iceGatheringState, candidates};
        } finally { pc.close(); }""", address[1], timeout_ms=10000)
        try:
            packet = await asyncio.wait_for(loop.sock_recv(stun, 2048), .2)
        except TimeoutError:
            assert gathering["result"]["state"] in ("gathering", "complete"), gathering
            assert "udp" not in gathering["result"]["candidates"], gathering
            print("WebRTC", gathering["result"]["state"], "no direct UDP", flush=True)
            return
        raise AssertionError(f"Chromium sent {len(packet)} bytes directly to the UDP canary")


async def check(root, benchmark=False):
    store = State(root)
    stage = Stage(root)
    # 8767 serves the isolated positive-control site. Real stages register
    # their listeners only after binding. The page-control guard must still
    # recognize 8767 as the external page under test.
    stage.page_network = PageNetworkPolicy(stage_ports={8766, 8767})
    site = web.Application()

    submissions = []
    blocked_connections = []

    def trap(reader, writer):
        blocked_connections.append(writer.get_extra_info("peername"))
        writer.close()

    async def events(request):
        return web.Response(text="data: ready\n\n", content_type="text/event-stream",
                            headers={"Access-Control-Allow-Origin": "*"})

    async def websocket(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for _ in ws:
            pass
        return ws

    async def content(request):
        if request.path == "/redirect":
            raise web.HTTPFound("/direct")
        if request.path == "/submitted":
            submissions.append(request.query_string)
        if request.path.startswith("/browser-error") and "Chrom" in request.headers.get(
            "User-Agent", ""
        ):
            # The app's GET succeeds. Only Chromium sees the blocked redirect.
            raise web.HTTPFound("http://127.0.0.1:8765/unreachable")
        if request.path == "/probe-redirect":
            raise web.HTTPFound("http://blocked.page.test:8765/unreachable")
        headers = (
            {"X-Frame-Options": "DENY"}
            if request.path in ("/direct", "/auth", "/browser-error-direct")
            else {}
        )
        headers["Content-Security-Policy"] = "script-src 'nonce-fixture'"
        return web.Response(
            text=PAGE,
            content_type="text/html",
            headers=headers,
            status=401 if request.path == "/auth" else 200,
        )

    site.router.add_get("/socket", websocket)
    site.router.add_get("/events", events)
    site.router.add_get("/{name}", content)
    stage_server, site_server = web.AppRunner(stage.app()), web.AppRunner(site)

    # Substitute tvpipe's browser sink for its AirPlay receiver. Keep the real
    # machine protocol, profile, capture tab and navigation code.
    executable = root / "tvpipe-browser"
    executable.write_text("""#!/usr/bin/env python3
import os,sys
args=sys.argv[1:]
i=args.index('--to')+1
assert args[i]=='127.0.0.1:7000'
args[i]='browser'
os.execvp('tvpipe',['tvpipe',*args,'--port','0'])
""")
    executable.chmod(0o700)
    mirror = TvpipeSender("127.0.0.1", store, str(executable), ready=stage.ready, stage=stage)
    spawn = browsers.spawn
    video_frames = {}

    async def observe_tvpipe(*args, **kwargs):
        proc = await spawn(*args, **kwargs)
        readline = proc.stdout.readline

        async def read_event():
            line = await readline()
            if line:
                event = json.loads(line)
                if event["type"] == "stats":
                    video_frames[proc.pid] = event["video_frames"]
            return line

        proc.stdout.readline = read_event
        return proc

    ops = Tools(None, stage, mirror, None, store)
    ops.live_mirror = mirror
    tools = PublicTools(ops, mirror.size)
    assert socket.gethostbyname("blocked.page.test") == "127.0.0.1"
    trap_server = await asyncio.start_server(trap, "127.0.0.1", 8765)
    observe = patch.object(browsers, "spawn", observe_tvpipe)
    observe.start()
    try:
        await stage_server.setup()
        await site_server.setup()
        await web.TCPSite(stage_server, "127.0.0.1", 8766).start()
        await web.TCPSite(site_server, "127.0.0.1", 8767).start()
        runner = None
        for label, source in [
            (
                "inline",
                {"html": PAGE},
            ),
            ("live frame", {"url": "http://127.0.0.1:8767/frame"}),
            ("direct", {"url": "http://127.0.0.1:8767/redirect"}),
            ("HTTP 401 page", {"url": "http://127.0.0.1:8767/auth"}),
            ("back on stage", {"files": BUNDLE}),
        ]:
            if benchmark and label != "live frame":
                continue
            options = dict(
                url=None, html=None, files=None, bundle=None, title=label, audio="mute"
            )
            await ops.present(**(options | source))
            if benchmark:
                frame = await mirror.presented_frame(stage.page["page_id"], stage.page["revision"], False)
                await exercise_large_page(tools, frame)
                return
            capture = await mirror.browser_page()
            assert capture.url == (
                "http://127.0.0.1:8767/direct" if label == "direct" else source["url"] if label == "HTTP 401 page" else STAGE_URL
            ), capture.url
            check_listener(mirror.profile, mirror.proxy.url)
            direct = stage.direct_browser is mirror
            frame = await mirror.presented_frame(stage.page["page_id"], stage.page["revision"], direct)
            assert (await run(tools, "window.setStep(3)"))["result"] == "Step 3"
            if label == "inline":
                await exercise_limits(tools)
                await exercise_egress(tools, mirror)
                await verify_capture(mirror, video_frames)
            if label in ("live frame", "direct"):
                await exercise_outline(tools, frame, direct)
                await exercise_scripts(tools, frame, submissions)
            if label == "live frame":
                await exercise_large_page(tools, frame)
            if label == "direct":
                await verify_capture(mirror, video_frames)
                await exercise_egress(tools, mirror)
                await exercise_webrtc(tools)
                async with capture.expect_popup() as opened:
                    await capture.evaluate("() => { window.open(location.href) }")
                popup = await opened.value
                await popup.wait_for_load_state()
                await popup.evaluate("document.title = 'Popup only'")
                assert (await run(tools, "document.title"))["result"] == "Recipe"
                await popup.close()
                previous = mirror.proc
                previous.terminate()
                async with asyncio.timeout(25):
                    while mirror.proc is previous or not mirror.started:
                        await asyncio.sleep(.1)
                assert (await run(tools, "document.title"))["result"] == "Recipe"
                await verify_capture(mirror, video_frames)
            if label == "back on stage":
                await exercise_bundle(tools, frame)
                await asyncio.sleep(.26)
                navigation = await tools.call("tv_page_run", {
                    "script": "location.href = arg", "arg": "http://127.0.0.1:8765/denied",
                })
                assert navigation["ok"] or navigation["error"]["code"] == "stale_view", navigation
                async with asyncio.timeout(5):
                    while not mirror.blocked_navigation(frame):
                        await asyncio.sleep(.05)
                await asyncio.sleep(.26)
                denial = await tools.call("tv_page_run", {"script": "document.title"})
                assert denial["error"]["code"] == "blocked_address", denial
            if runner:
                assert runner is mirror.runner
            runner = mirror.runner
            print(label, "script confirmed", flush=True)
        bundle = stage.page["page_id"]
        await ops.present(**(options | {"url": f"http://127.0.0.1:8766/c/{bundle}/index.html"}))
        await asyncio.sleep(1)
        blocked = await tools.call("tv_page_run", {"script": "document.title"})
        assert blocked["error"]["code"] == "target_missing", blocked
        for url in ("http://127.0.0.1:8765/api/health", "http://127.0.0.1:8767/probe-redirect"):
            # Call the tool handler directly. Its outer dispatcher wakes the TV.
            try:
                await tools.tv_show(url=url)
            except Fault as exc:
                assert exc.code == "blocked_address" and "page_network_allow" in exc.remediation, exc
            else:
                raise AssertionError(f"tv_show loaded {url}")
        # Check the redirect before any DevTools page listeners from a previous
        # presentation can observe it. Each failed show stops its browser.
        await ops.tv_hide()
        for endpoint in ("browser-error", "browser-error-direct"):
            try:
                await ops.present(**(options | {"url": "http://127.0.0.1:8767/" + endpoint}))
            except Fault as exc:
                assert exc.code == "blocked_address" and stage.page is None, (endpoint, exc)
                assert "page_network_allow" in exc.remediation, exc
                print(endpoint, str(exc), flush=True)
            else:
                raise AssertionError(f"{endpoint} was reported loaded")
        assert not blocked_connections, blocked_connections
    finally:
        observe.stop()
        await mirror.stop()
        trap_server.close()
        await trap_server.wait_closed()
        await site_server.cleanup()
        await stage_server.cleanup()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(check(Path(root), "--benchmark" in sys.argv))
