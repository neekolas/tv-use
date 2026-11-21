import base64
import io
import sys
from collections import deque
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from atv_bridge_app.atv.discovery import Resolver
from atv_bridge_app.processes import ObserverClient
from atv_bridge_app.server import Service
from atv_bridge_app.state import State


class FakeTV:
    def __init__(self, power="on"):
        self.power_state = power
        self.keys = []
        self.targets = []
        self.closed = False
        self.lose_key = None
        self.media = None
        self.app = None

    async def hid(self, key, hold):
        self.keys.append(key)
        if key == self.lose_key:
            raise ConnectionResetError()

    async def close(self):
        self.closed = True

    async def power(self):
        return self.power_state

    async def status(self):
        return {
            "power": self.power_state,
            "app": self.app,
            "now_playing": self.media,
            "keyboard_focused": True,
            "volume_supported": True,
            "tvos": "test",
        }

    async def apps(self):
        return [{"name": "YouTube", "bundle_id": "com.google.ios.youtube", "allowed": True}]

    async def open(self, target):
        self.targets.append(target)
        self.app = (await self.apps())[0]
        if target.startswith("youtube://watch/"):
            self.media = {
                "state": "playing",
                "title": "Example",
                "content_identifier": target.split("/")[-1],
            }

    async def type(self, text, clear):
        return True


class FakeAdapter:
    def __init__(self, state):
        self.state = state
        self.host = "192.0.2.1"
        self.resolver = Resolver(self.host, state)
        self.resolver.host = AsyncMock(return_value=self.host)
        self.resolver.airplay_endpoint = AsyncMock(return_value=(self.host, 7000))
        self.resolver.scan = AsyncMock(return_value=[])
        self.access_restricted = False
        self.clients = []
        self.power_sequence = deque()
        self.callback = None

    async def connect(self, callback, companion_only=False):
        assert all(tv.closed for tv in self.clients)
        self.callback = callback
        tv = FakeTV(self.power_sequence.popleft() if self.power_sequence else "on")
        self.clients.append(tv)
        return tv


@pytest.fixture
async def service(tmp_path):
    store = State(tmp_path)
    adapter = FakeAdapter(store)
    buf = io.BytesIO()
    Image.new("RGB", (1, 1), "blue").save(buf, "PNG")
    png = base64.b64encode(buf.getvalue()).decode()
    script = """import json,sys,os
for line in sys.stdin:
    cmd = json.loads(line)
    if cmd["method"] == "buttons_start":
        # The real observer reports the watch once the TV accepts its log stream.
        print(json.dumps({"event": "buttons", "state": "active"}), flush=True)
    data = {"screenshot": {"png": sys.argv[1]}, "ax_focused": {"focused": {"caption": "TV"}},
            "ax_items": {"items": [{"caption":"TV","spoken_description":"TV","uid":"tile-1"}], "truncated": True},
            "ax_running_apps": {"apps": [{"bundle_id":"com.apple.TV","name":"TV","pid":1}]}}
    print(json.dumps({"ok": True, "pid": os.getpid(), **data.get(cmd["method"], {})}), flush=True)
"""
    observer = ObserverClient(adapter.host, store, [sys.executable, "-c", script, png])
    result = Service(adapter.host, store, adapter, observer)
    result.engine.wake_timeout = 0.015
    result.operations.verify_timeout = 0
    result.tools.play_timeout = 0
    result.tools.settle_ms = 0
    yield result
    await result.tools.remote_input.close()
    await result.observer.close()
    await result.tools.media.stop()
    await result.engine.stop()
