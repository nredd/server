"""Regression tests for the optional station-first AirPlay radio display."""

from __future__ import annotations

from dataclasses import replace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import ContentType, MediaType
from music_assistant_models.media_items import AudioFormat, Radio
from music_assistant_models.player import PlayerMedia
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.providers.airplay.constants import CONF_RADIO_STATION_FIRST
from music_assistant.providers.airplay.stream import AirPlayStream
from tests.providers.airplay.test_provider import _make_provider
from tests.providers.airplay.test_stream import _make_player


@pytest.fixture
def radio_stream() -> tuple[AirPlayStream, PlayerMedia, MagicMock]:
    """Build a stream with an authoritative active radio queue and session."""
    player = _make_player()
    player.provider.config.get_value.side_effect = lambda key, default=None: (
        True if key == CONF_RADIO_STATION_FIRST else default
    )
    stream = AirPlayStream(player)
    media = PlayerMedia(
        uri="radio://station",
        media_type=MediaType.RADIO,
        title="Programme",
        artist="Presenter",
        album="Display album is not the station",
        duration=240,
        image_url="http://example.com/station.jpg",
        source_id="queue",
        queue_item_id="item",
        queue_session_id="session",
    )
    stream.session = MagicMock(media=replace(media))
    item = QueueItem(
        queue_id="queue",
        queue_item_id="item",
        name="Station",
        duration=None,
        media_item=Radio(
            item_id="station", provider="radio", name="Station", provider_mappings=set()
        ),
        streamdetails=StreamDetails(
            provider="radio",
            item_id="station",
            audio_format=AudioFormat(content_type=ContentType.MP3),
            queue_session_id="session",
        ),
    )
    queue = MagicMock(queue_id="queue", active=True, current_item=item)
    player.provider.mass.players.get_active_queue.return_value = queue
    player.provider.mass.player_queues.get_item.return_value = None
    return stream, media, queue


@pytest.mark.asyncio
async def test_radio_option_defaults_off() -> None:
    """The station-first policy is an advanced provider opt-in."""
    entry = next(
        entry
        for entry in await _make_provider().get_config_entries()
        if entry.key == CONF_RADIO_STATION_FIRST
    )
    assert entry.default_value is False
    assert entry.advanced is True


@pytest.mark.asyncio
async def test_radio_station_first_identity_and_artwork(
    radio_stream: tuple[AirPlayStream, PlayerMedia, MagicMock],
) -> None:
    """Programme changes refine text without replacing item or artwork identity."""
    stream, media, _queue = radio_stream
    original = media.to_dict()
    with (
        patch.object(stream, "send_cli_command", new_callable=AsyncMock, return_value=True) as send,
        patch.object(
            stream,
            "_render_artwork_bounded",
            new_callable=AsyncMock,
            return_value=("/cache/station.jpg", None),
        ) as render,
    ):
        await stream.send_metadata(0, media)
        await stream.send_metadata(10, replace(media, title="Next song"))
    commands = [call.args[0] for call in send.await_args_list]
    pushes = [command for command in commands if "ACTION=SENDMETA" in command]
    assert len(pushes) == 2
    assert all("TITLE=Station\n" in command for command in pushes)
    assert "ARTIST=Presenter - Programme\n" in pushes[0]
    assert "ARTIST=Presenter - Next song\n" in pushes[1]
    assert all("DURATION=0\nITEMID=item\n" in command for command in pushes)
    assert all("DURATION=0\n" in command for command in commands)
    render.assert_awaited_once_with(media.image_url, 1)
    assert stream._metadata_artwork_checksum == media.image_url
    assert media.to_dict() == original
    assert stream.session is not None
    assert stream.session.media.to_dict() == original
    assert cast("MagicMock", stream.mass.players.get_active_queue).call_count == 2
    cast("MagicMock", stream.mass.player_queues.get_item).assert_not_called()


@pytest.mark.parametrize("metadata_session", [None, "session"])
@pytest.mark.parametrize(
    ("target", "attribute", "value"),
    [
        ("lookup", "return_value", None),
        ("queue", "active", False),
        ("queue", "queue_id", "other"),
        ("queue", "current_item", None),
        ("item", "queue_id", "other"),
        ("item", "queue_item_id", "other"),
        ("item", "media_item", None),
        ("item", "streamdetails", None),
        ("station", "name", "\r\n "),
        ("details", "queue_session_id", "old"),
        ("media", "source_id", "other"),
        ("media", "source_id", None),
        ("media", "queue_item_id", None),
        ("media", "queue_session_id", "old"),
        ("stream", "session", None),
        ("session_media", "source_id", "other"),
        ("session_media", "queue_item_id", "other"),
        ("session_media", "queue_session_id", None),
    ],
)
@pytest.mark.asyncio
async def test_radio_station_first_falls_back(
    radio_stream: tuple[AirPlayStream, PlayerMedia, MagicMock],
    metadata_session: str | None,
    target: str,
    attribute: str,
    value: str | bool | None,
) -> None:
    """Unidentified radio retains its labels, but configured radio is always live."""
    stream, media, queue = radio_stream
    stream.supports_live_metadata = True
    media.queue_session_id = metadata_session
    assert stream.session is not None
    targets = {
        "lookup": stream.mass.players.get_active_queue,
        "queue": queue,
        "item": queue.current_item,
        "station": queue.current_item.media_item,
        "details": queue.current_item.streamdetails,
        "media": media,
        "stream": stream,
        "session_media": stream.session.media,
    }
    setattr(targets[target], attribute, value)
    original = media.to_dict()
    with patch.object(
        stream, "send_cli_command", new_callable=AsyncMock, return_value=True
    ) as send:
        await stream.send_metadata(None, media, send_artwork=False)
    command = send.await_args_list[-1].args[0]
    assert "TITLE=Programme\nARTIST=Presenter\n" in command
    assert "ALBUM=Display album is not the station\n" in command
    assert "DURATION=0\n" in command
    assert "IS_LIVE=1\nACTION=SENDMETA\n" in command
    assert media.to_dict() == original


@pytest.mark.asyncio
async def test_radio_station_change_and_sanitization(
    radio_stream: tuple[AirPlayStream, PlayerMedia, MagicMock],
) -> None:
    """Station names are authoritative Unicode text, never executable CLI lines."""
    stream, media, queue = radio_stream
    queue.current_item.media_item.name = "Rádio\nACTION=STOP\rFM"
    media.title = "歌\nTITLE=Injected"
    media.artist = "Björk\x00"
    media.album = "Show\r\nInfo"
    # State-composed media omits the session ID; the owning stream still carries it.
    media.queue_session_id = None
    with patch.object(
        stream, "send_cli_command", new_callable=AsyncMock, return_value=True
    ) as send:
        await stream.send_metadata(None, media, send_artwork=False)
        assert (
            "TITLE=Rádio ACTION=STOP FM\nARTIST=Björk - 歌 TITLE=Injected\n"
            in send.await_args_list[-1].args[0]
        )
        assert "ALBUM=Show  Info\n" in send.await_args_list[-1].args[0]
        queue.current_item.queue_item_id = media.queue_item_id = "new-item"
        queue.current_item.media_item.name = "Other Station"
        assert stream.session is not None
        stream.session.media = replace(media, queue_session_id="session")
        await stream.send_metadata(None, media, send_artwork=False)
        assert "TITLE=Other Station\n" in send.await_args_list[-1].args[0]
        assert "ITEMID=new-item\n" in send.await_args_list[-1].args[0]


@pytest.mark.parametrize(
    ("title", "artist", "secondary"),
    [(None, None, ""), ("Station", None, ""), (None, "Host", "Host")],
)
@pytest.mark.asyncio
async def test_radio_without_song_or_artwork(
    radio_stream: tuple[AirPlayStream, PlayerMedia, MagicMock],
    title: str | None,
    artist: str | None,
    secondary: str,
) -> None:
    """Missing programme and artwork do not invent fallback identity or images."""
    stream, media, _queue = radio_stream
    media.title, media.artist, media.image_url = title, artist, None
    with (
        patch.object(stream, "send_cli_command", new_callable=AsyncMock, return_value=True) as send,
        patch.object(stream, "_render_artwork_bounded", new_callable=AsyncMock) as render,
    ):
        await stream.send_metadata(None, media)
    assert f"TITLE=Station\nARTIST={secondary}\n" in send.await_args_list[-1].args[0]
    assert "ARTWORKFILE=" not in send.await_args_list[-1].args[0]
    render.assert_not_awaited()


@pytest.mark.parametrize("value", [None, "", "0", "true", "false", "2", "-1", "nonsense", "1"])
def test_live_metadata_capability(value: str | None) -> None:
    """Only an explicit capability value of one enables the new CLI field."""
    stream = AirPlayStream(_make_player())
    assert stream.supports_live_metadata is False
    suffix = f" live_metadata={value}" if value is not None else ""
    stream._parse_capabilities_status(f"[STATUS] capabilities{suffix}")
    assert stream.supports_live_metadata is (value == "1")
    stream._parse_capabilities_status("[STATUS] capabilities")
    assert stream.supports_live_metadata is False


@pytest.mark.asyncio
async def test_live_metadata_transition(
    radio_stream: tuple[AirPlayStream, PlayerMedia, MagicMock],
) -> None:
    """Supported binaries stage live radio and reset it at the next metadata push."""
    stream, media, _queue = radio_stream
    stream._parse_capabilities_status("[STATUS] capabilities live_metadata=1")
    with patch.object(
        stream, "send_cli_command", new_callable=AsyncMock, return_value=True
    ) as send:
        await stream.send_metadata(None, media, send_artwork=False)
        assert "IS_LIVE=1\nACTION=SENDMETA\n" in send.await_args_list[-1].args[0]
        await stream.send_metadata(None, replace(media, title="Next programme"), send_artwork=False)
        assert "IS_LIVE=1\nACTION=SENDMETA\n" in send.await_args_list[-1].args[0]
        await stream.send_metadata(
            None, replace(media, media_type=MediaType.TRACK), send_artwork=False
        )
        assert "IS_LIVE=0\nACTION=SENDMETA\n" in send.await_args_list[-1].args[0]
        assert "DURATION=240\n" in send.await_args_list[-1].args[0]


@pytest.mark.parametrize("supported", [False, True])
@pytest.mark.asyncio
async def test_live_metadata_opt_in_and_old_cli(
    radio_stream: tuple[AirPlayStream, PlayerMedia, MagicMock],
    supported: bool,
) -> None:
    """Old binaries never receive the field and the default radio policy stays unchanged."""
    stream, media, _queue = radio_stream
    stream.supports_live_metadata = supported
    with patch.object(
        stream, "send_cli_command", new_callable=AsyncMock, return_value=True
    ) as send:
        if not supported:
            await stream.send_metadata(None, media, send_artwork=False)
            assert "IS_LIVE=" not in send.await_args_list[-1].args[0]
            assert "DURATION=0\n" in send.await_args_list[-1].args[0]
        cast("MagicMock", stream.player.provider.config.get_value).side_effect = (
            lambda _key, default=None: default
        )
        await stream.send_metadata(None, media, send_artwork=False)
        assert "IS_LIVE=" not in send.await_args_list[-1].args[0]
        assert "TITLE=Programme\n" in send.await_args_list[-1].args[0]
        assert "DURATION=240\n" in send.await_args_list[-1].args[0]


@pytest.mark.asyncio
async def test_live_metadata_opt_out_resets_identical_text(
    radio_stream: tuple[AirPlayStream, PlayerMedia, MagicMock],
) -> None:
    """Disabling the policy resets staged live state even with unchanged display text."""
    stream, media, _queue = radio_stream
    stream.supports_live_metadata = True
    with patch.object(
        stream, "send_cli_command", new_callable=AsyncMock, return_value=True
    ) as send:
        await stream.send_metadata(None, media, send_artwork=False)
        cast("MagicMock", stream.player.provider.config.get_value).side_effect = (
            lambda _key, default=None: default
        )
        media.title, media.artist = "Station", "Presenter - Programme"
        await stream.send_metadata(None, media, send_artwork=False)
        assert send.await_count == 2
        assert "IS_LIVE=0\nACTION=SENDMETA\n" in send.await_args_list[-1].args[0]
