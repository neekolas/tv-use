"""One AirPlay URL playback job. Resume interrupted streams and stop at normal EOF."""

import asyncio
from ..state import Fault, classify


class MediaPlayback:
    def __init__(self, engine):
        self.engine = engine
        self.task = None
        self.problem = None

    @property
    def active(self):
        return self.task is not None and not self.task.done()

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        self.problem = None

    async def start(self, url):
        await self.stop()
        if not getattr(self.engine.adapter, "supports_play_url", False):
            raise Fault(
                "launch_refused",
                "This build lacks AirPlay URL playback.",
                "Install the app with the staged pyatv fork.",
            )
        self.task = asyncio.create_task(self._run(url))

    async def _run(self, url):
        position = 0
        try:
            for attempt in range(3):
                async with self.engine.session(fresh=attempt > 0) as client:
                    play = asyncio.create_task(client.play_url(url, int(position)))
                elapsed = position
                duration = None
                clock = asyncio.get_running_loop()
                stamp = clock.time()
                try:
                    while not play.done():
                        await asyncio.wait((play,), timeout=0.5)
                        snapshot = await self.engine.status()
                        media = snapshot.get("now_playing") or {}
                        current = clock.time()
                        if snapshot.get("airplay_playing"):
                            reported = media.get("position_s")
                            elapsed = (
                                max(0, reported)
                                if reported is not None
                                else elapsed + current - stamp
                            )
                            duration = media.get("duration_s") or duration
                        stamp = current
                    interrupted = await play
                    if interrupted is False or (
                        interrupted is None and (not duration or elapsed >= duration - 2)
                    ):
                        return
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                except Exception as exc:
                    if not classify(exc).retryable or attempt == 2:
                        raise
                finally:
                    play.cancel()
                    await asyncio.gather(play, return_exceptions=True)
                position = elapsed
                await asyncio.sleep(1 + attempt)
            raise Fault(
                "uncertain", "AirPlay repeatedly closed early.", "Check playback before retrying."
            )
        except Exception as exc:  # noqa: BLE001 - Retain failures for the caller.
            self.problem = classify(exc)
