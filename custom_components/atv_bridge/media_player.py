"""Apple TV playback and app selection."""

from datetime import datetime
from urllib.parse import urlsplit

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerState,
    MediaType,
)
from homeassistant.components.media_player import MediaPlayerEntityFeature as Feature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.dt import parse_datetime
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from . import receivers
from .coordinator import BridgeEntry
from .entity import BridgeEntity, PowerCommands

# MediaType.URL covers both AirPlay assets and links that open a tvOS app.
APP_LINK_HOSTS = {
    "tv.apple.com",
    "music.apple.com",
    "open.spotify.com",
    "www.netflix.com",
    "netflix.com",
    "disneyplus.com",
    "www.disneyplus.com",
}
YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "youtu.be",
    "m.youtube.com",
    "music.youtube.com",
}


BASE_FEATURES = (
    Feature.TURN_ON
    | Feature.TURN_OFF
    | Feature.PLAY
    | Feature.PAUSE
    | Feature.NEXT_TRACK
    | Feature.PREVIOUS_TRACK
    | Feature.SELECT_SOURCE
    | Feature.PLAY_MEDIA
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BridgeEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    for coordinator in receivers(entry):
        async_add_entities([BridgePlayer(coordinator, "player")], config_subentry_id=coordinator.subentry_id)


class BridgePlayer(PowerCommands, BridgeEntity, MediaPlayerEntity):
    @property
    def supported_features(self) -> Feature:
        supported = BASE_FEATURES
        controls = self.status.get("playback_controls", {})
        if self.status.get("volume_supported"):
            supported |= Feature.VOLUME_STEP
        if controls.get("seek_absolute"):
            supported |= Feature.SEEK
        if controls.get("stop"):
            supported |= Feature.STOP
        if controls.get("set_volume"):
            supported |= Feature.VOLUME_SET
        return supported

    @property
    def state(self) -> MediaPlayerState | None:
        if self.status.get("power") == "off":
            return MediaPlayerState.OFF
        if self.status.get("power") in ("on", "screensaver"):
            return {
                "playing": MediaPlayerState.PLAYING,
                "paused": MediaPlayerState.PAUSED,
            }.get(self.playing.get("state"), MediaPlayerState.IDLE)
        return None

    @property
    def playing(self) -> dict:
        return self.status.get("now_playing") or {}

    @property
    def media_position(self) -> int | None:
        return self.playing.get("position_s")

    @property
    def media_duration(self) -> int | None:
        return self.playing.get("duration_s")

    @property
    def media_position_updated_at(self) -> datetime | None:
        stamp = self.status.get("position_updated_at")
        return parse_datetime(stamp) if stamp and self.media_position is not None else None

    @property
    def volume_level(self) -> float | None:
        percent = self.status.get("volume_percent")
        return None if percent is None else percent / 100

    @property
    def media_title(self) -> str | None:
        return self.playing.get("title")

    @property
    def media_artist(self) -> str | None:
        return self.playing.get("artist")

    @property
    def app_id(self) -> str | None:
        return (self.status.get("app") or {}).get("bundle_id")

    @property
    def source(self) -> str | None:
        return (self.status.get("app") or {}).get("name")

    @property
    def source_list(self) -> list[str]:
        return self.coordinator.data.apps

    async def async_select_source(self, source: str) -> None:
        await self.command("tv_open", app=source)

    async def async_play_media(self, media_type: str, media_id: str, **kwargs) -> None:
        if media_type not in (MediaType.APP, MediaType.URL):
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="unsupported_media"
            )
        if media_type == MediaType.APP:
            await self.command("tv_open", app=media_id)
        else:
            url = urlsplit(media_id)
            if url.hostname in YOUTUBE_HOSTS or url.scheme == "youtube":
                await self.command("tv_play", youtube=media_id)
            elif url.hostname in APP_LINK_HOSTS or url.scheme not in ("http", "https"):
                await self.command("tv_play", link=media_id)
            else:
                await self.command("tv_play", media=media_id)

    async def async_media_play(self) -> None:
        await self.command("tv_remote", keys=["play"])

    async def async_media_pause(self) -> None:
        await self.command("tv_remote", keys=["pause"])

    async def async_media_next_track(self) -> None:
        await self.command("tv_remote", keys=["next"])

    async def async_media_previous_track(self) -> None:
        await self.command("tv_remote", keys=["previous"])

    async def async_media_seek(self, position: float) -> None:
        await self.command("tv_seek", position_s=round(position))

    async def async_media_stop(self) -> None:
        await self.command("tv_remote", keys=["stop"])

    async def async_set_volume_level(self, volume: float) -> None:
        await self.command("tv_volume", percent=volume * 100)

    async def async_volume_up(self) -> None:
        await self.command("tv_remote", keys=["volume_up"])

    async def async_volume_down(self) -> None:
        await self.command("tv_remote", keys=["volume_down"])
