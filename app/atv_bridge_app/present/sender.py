"""Presentation senders share lifecycle, status and state-change events."""

import asyncio
from abc import ABC, abstractmethod

from ..state import Fault, success

STAGE_URL = "http://127.0.0.1:8766/stage"


class Events:
    def __init__(self):
        self._listeners = []

    def subscribe(self, listener):
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def emit(self, status):
        for listener in tuple(self._listeners):
            listener(status)


class Sender(ABC):
    def __init__(self, mode):
        self.mode = mode
        self.events = Events()
        self._status = success("idle", mode=mode)

    def status(self):
        return self._status.copy()

    def update(self, status):
        self._status = status
        self.events.emit(self.status())

    def transition(self, state, evidence=(), **fields):
        self.update(success(state, evidence, mode=self.mode, **fields))

    def fail(self, problem):
        self.update(problem.result() | {"mode": self.mode})

    @abstractmethod
    async def start(self, stage_url):
        """Present the current revision of the stage."""

    @abstractmethod
    async def stop(self):
        """Stop presenting and release owned resources."""


class AutoSender(Sender):
    def __init__(self, mirror, play_url):
        super().__init__("auto")
        self.mirror = mirror
        self.play_url = play_url
        self.active = mirror
        self.recovery = None
        self.stage_url = None
        for sender in (mirror, play_url):
            sender.events.subscribe(lambda status, owner=sender: self._forward(owner, status))

    def _forward(self, owner, status):
        if self.active is owner:
            self.update(status | {"configured_mode": "auto"})
            if owner is self.mirror and not status["ok"] and self.stage_url:
                # The mirror had started successfully, then its restart after a
                # failure failed. That failure counts as the first of the two.
                # A mirror that the TV ended reports an ok "ended" state instead.
                stage_url, self.stage_url = self.stage_url, None
                self.recovery = asyncio.create_task(self._recover(stage_url))

    async def _recover(self, stage_url):
        try:
            await self.mirror.stop()
            await self._start(stage_url, mirror_attempts=1)
        except Exception as exc:  # noqa: BLE001 - Background restart failures go to sender status.
            await self.active.stop()
            problem = (
                exc
                if isinstance(exc, Fault)
                else Fault(
                    "sender_failed",
                    "Presentation restart failed.",
                    "Check AirPlay and the stage.",
                    True,
                )
            )
            self.active.fail(problem)

    async def release_supervision(self):
        """Cancel automatic fallback when another presentation takes over the active mirror."""
        self.stage_url = None
        if self.recovery:
            self.recovery.cancel()
            await asyncio.gather(self.recovery, return_exceptions=True)
            self.recovery = None

    async def _start(self, stage_url, mirror_attempts):
        if self.active is self.mirror:
            for _ in range(mirror_attempts):
                try:
                    # HA's REST client allows 60 seconds for a call. Leave time
                    # for both failed children to shut down and the fallback to start.
                    async with asyncio.timeout(18):
                        await self.mirror.start(stage_url)
                    self.stage_url = stage_url
                    return
                except (Fault, TimeoutError, OSError):
                    await self.mirror.stop()
            self.active = self.play_url
        await self.active.start(stage_url)

    async def start(self, stage_url):
        await self.release_supervision()
        await self._start(stage_url, mirror_attempts=2)

    async def stop(self):
        await self.release_supervision()
        await self.active.stop()
        self.active = self.mirror
        self.transition("idle")
