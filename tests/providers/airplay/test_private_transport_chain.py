"""
Real HTTP/queue/controller/provider locks over fake receiver I/O.

References:
    https://docs.python.org/3/library/asyncio-task.html
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import weakref
from collections.abc import AsyncGenerator, Coroutine
from contextlib import asynccontextmanager
from types import MethodType, SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.enums import MediaType, PlaybackState, PlayerType
from music_assistant_models.media_items import ProviderMapping, Radio
from music_assistant_models.player_queue import PlayerQueue

from music_assistant.controllers.player_queues import controller as queue_module
from music_assistant.controllers.player_queues.controller import PlayerQueuesController
from music_assistant.controllers.player_queues.media_resolver import MediaResolver
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.players.controller import PlayerController
from music_assistant.controllers.webserver.controller import WebserverController
from music_assistant.helpers.api import APICommandHandler
from music_assistant.helpers.uri import parse_uri
from music_assistant.models.player import PlayerMedia
from music_assistant.providers.airplay import player as airplay_module
from music_assistant.providers.airplay.provider import AirPlayProvider

if TYPE_CHECKING:
    from music_assistant import MusicAssistant

LOGGER = logging.getLogger(__name__)
PLAYER = "ap021122334455"
URL = "http://192.0.2.1:18080/stimulus.wav"
TOKEN = "a" * 32


def completes_without_suspending(coro: Coroutine[Any, Any, Any]) -> Any:
    """Assert the actual classifier returns without a scheduling yield."""
    try:
        with pytest.raises(StopIteration) as completed:
            coro.send(None)
        return completed.value.value
    finally:
        coro.close()


def test_actual_http_preclassification_does_not_suspend() -> None:
    """Do not resurrect the withdrawn private HTTP pre-lock late-PLAY claim."""
    assert completes_without_suspending(queue_module._resolve_play_sort(URL, None, None, None)) == (
        None,
        None,
    )
    assert completes_without_suspending(parse_uri(URL)) == (MediaType.UNKNOWN, "builtin", URL)
    assert completes_without_suspending(queue_module._resolve_audio_source_request(URL)) is None


@asynccontextmanager
async def prepare_media() -> AsyncGenerator[None]:
    """Represent fake preparation with no power/device operations."""
    yield


class Chain:
    """Real forwarding and locks; fake authentication, metadata, persistence and receiver."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Set up a receiver-free controller chain."""
        self.entered, self.release, self.stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.background: list[asyncio.Task[Any]] = []
        self.mass: Any = SimpleNamespace(
            cancel_timer=lambda *_args: None,
            cancel_task=lambda *_args: None,
            config=SimpleNamespace(get_raw_player_config_value=lambda *_args: None),
            translations=SimpleNamespace(ensure_locale_loaded=AsyncMock()),
            create_task=self.create_task,
        )
        self.players: Any = PlayerController.__new__(PlayerController)
        self.queues: Any = PlayerQueuesController.__new__(PlayerQueuesController)
        self.players.mass = cast("MusicAssistant", self.mass)
        self.queues.mass = cast("MusicAssistant", self.mass)
        self.players.logger = self.queues.logger = LOGGER
        self.mass.players, self.mass.player_queues = self.players, self.queues
        self.players._player_command_locks = {}
        self.players._task_held_locks = weakref.WeakKeyDictionary()
        self.players._state_update_subscribers = []
        self.players._source_sessions = {}
        self.queue = PlayerQueue(PLAYER, True, "synthetic TV", True, 0)
        self.data = PlayerQueueData(self.queue)
        self._setup_queue()
        self._setup_player()
        self._setup_http(monkeypatch)

    def create_task(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        """Retain ordinary queue cleanup tasks for test teardown."""
        task = asyncio.create_task(coro)
        self.background.append(task)
        return task

    async def load(self, _queue_id: str, queue_items: list[Any], **_kwargs: Any) -> None:
        """Replace persistence only, not enqueue or forwarding."""
        self.data.items = queue_items
        self.queue.items = len(queue_items)

    async def metadata(self, uri: str) -> Radio:
        """Fake source lookup while asserting the real controller lock is held."""
        assert self.players._task_held_locks.get(asyncio.current_task())
        assert uri == URL
        return Radio(
            item_id=URL,
            provider="builtin",
            name="synthetic WAV",
            provider_mappings={
                ProviderMapping(item_id=URL, provider_domain="builtin", provider_instance="builtin")
            },
        )

    async def http(self, command: str, args: dict[str, Any]) -> Any:
        """Use actual JSON/argument parsing with synthetic authentication."""
        request: Any = SimpleNamespace(
            remote="127.0.0.1",
            can_read_body=True,
            headers={},
            read=AsyncMock(return_value=json.dumps({"command": command, "args": args}).encode()),
        )
        return await WebserverController._handle_jsonrpc_api_command(self.web, request)

    async def private(self, mode: str) -> Any:
        """Dispatch an actual private HTTP command."""
        request = {"mode": mode, "attempt_id": TOKEN}
        if mode == "arm":
            request |= {"player_id": PLAYER, "media": URL}
        return await self.http("airplay/private_trial_transport", {"request": request})

    def assert_drained(self) -> None:
        """Check actual controller/queue/provider lock bookkeeping after cancellation."""
        assert self.data.play_action_refcount == 0
        assert not self.players._task_held_locks
        assert not self.players._state_update_subscribers
        assert all(not lock.locked() for lock in self.players._player_command_locks.values())
        assert not self.player._lock.locked()

    def _setup_queue(self) -> None:
        """Replace bookkeeping/audio boundaries while keeping real queue actions."""
        queues: Any = self.queues
        queues._queue_data = {PLAYER: self.data}
        queues._media_resolver = MediaResolver(queues)
        queues._managed_pool = SimpleNamespace(retain=lambda *_args: None)
        queues.signal_update = lambda *_args: None
        queues.on_player_update = lambda *_args: None
        queues.is_smart_shuffle_active = lambda *_args: False
        queues._cleanup_queue_audio_data = AsyncMock()
        queues._load_item = AsyncMock()
        queues._get_resume_position = AsyncMock(return_value=0)
        queues.player_media_from_queue_item = AsyncMock(
            return_value=PlayerMedia(
                uri=URL,
                media_type=MediaType.RADIO,
                source_id=PLAYER,
            )
        )
        queues.load = self.load
        self.mass.music = SimpleNamespace(
            get_item_by_uri=self.metadata,
            check_item_playable_for_user=lambda *_args: None,
        )
        self.mass.streams = SimpleNamespace(
            get_stream=lambda *_args: "synthetic finite source",
            audio=SimpleNamespace(clear_crossfade_handover=lambda *_args: None),
            audio_processing=SimpleNamespace(
                start_session=lambda *_args: None, clear=lambda *_args: None
            ),
        )

    def _setup_player(self) -> None:
        """Use actual AirPlay playback/STOP methods and actual controller locks."""
        state = SimpleNamespace(
            available=True,
            enabled=True,
            synced_to=None,
            active_group=None,
            active_source=PLAYER,
            playback_state=PlaybackState.IDLE,
            powered=True,
            name="synthetic TV",
            group_members=[],
        )
        self.player: Any = SimpleNamespace(
            player_id=PLAYER,
            display_name="synthetic TV",
            protocol_parent_id=None,
            state=state,
            extra_data={},
            type=PlayerType.PLAYER,
            active_output_protocol=None,
            _lock=asyncio.Lock(),
            stream=None,
            synced_to=None,
            _get_sync_clients=list,
            _get_session_pcm_format=AsyncMock(return_value="pcm"),
            cancel_group_rejoin=lambda: None,
            mass=self.mass,
            prepare_play_media=prepare_media,
            set_active_mass_source=lambda value: setattr(state, "active_source", value),
            set_active_output_protocol=lambda value: setattr(
                self.player, "active_output_protocol", value
            ),
            mark_stop_called=lambda: None,
            update_state=lambda: None,
            provider=SimpleNamespace(
                bridge_manager=SimpleNamespace(stop_streaming=lambda *_args: False)
            ),
        )
        self.player.play_media = MethodType(airplay_module.AirPlayPlayer.play_media, self.player)
        self.player.stop = MethodType(airplay_module.AirPlayPlayer.stop, self.player)
        players: Any = self.players
        players._players = {PLAYER: self.player}
        players._release_audio_source = AsyncMock()
        players._regroup_off_unneeded_protocol = AsyncMock()
        players._select_best_output_protocol = lambda _player: (self.player, None)
        players.schedule_active_output_protocol_clear = lambda *_args: None

    def _setup_http(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Parse actual HTTP commands, with explicitly synthetic auth/readiness."""
        self.provider: Any = SimpleNamespace(
            _private_transport_attempt=None,
            _private_admission_epoch=0,
            get_players=lambda: [self.player],
            _private_transport_current=lambda *_args: True,
            mass=self.mass,
        )
        self.provider.private_trial_transport = MethodType(
            AirPlayProvider.private_trial_transport, self.provider
        )
        self.mass.command_handlers = {
            name: APICommandHandler.parse(name, method)
            for name, method in (
                ("player_queues/play_media", self.queues.play_media),
                ("players/cmd/stop", self.players.cmd_stop),
                ("airplay/private_trial_transport", self.provider.private_trial_transport),
            )
        }
        self.web: Any = SimpleNamespace(
            mass=self.mass,
            logger=LOGGER,
            auth=SimpleNamespace(has_users=True),
            _authenticate_api_command=AsyncMock(return_value=None),
            _localized_json_response=lambda *_args: SimpleNamespace(status=200),
        )
        chain = self

        class Session:
            """The only held I/O boundary, not a device or process."""

            def __init__(self, *_args: Any) -> None:
                """Accept the real session constructor shape."""

            async def start(self, _source: Any) -> None:
                """Hold both real playback locks until released or cancelled."""
                chain.entered.set()
                await chain.release.wait()
                chain.player.state.playback_state = PlaybackState.PLAYING
                chain.player.stream = SimpleNamespace(session=self, running=True)
                for callback in list(chain.players._state_update_subscribers):
                    callback(
                        chain.player,
                        {"playback_state": (PlaybackState.IDLE, PlaybackState.PLAYING)},
                    )

            async def private_release(self) -> None:
                """Record captured-session cleanup independently of playback locks."""
                chain.stopped.set()

            async def stop(self) -> None:
                """Record ordinary teardown after controller STOP forwards."""
                chain.player.state.playback_state = PlaybackState.IDLE
                chain.player.stream = None
                chain.stopped.set()

        monkeypatch.setattr(airplay_module, "AirPlayStreamSession", Session)
        monkeypatch.setattr(
            "music_assistant.providers.airplay.provider.is_trial_target", lambda: True
        )


@pytest.mark.parametrize("mode", ["stop", "expiry"])
async def test_private_http_cancels_held_start_before_locks(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """Private STOP/expiry cancels held real forwarding even without ordinary STOP."""
    chain = Chain(monkeypatch)
    await chain.private("arm")
    dispatch = time.monotonic()
    await chain.private("play")
    attempt = chain.provider._private_transport_attempt
    try:
        await asyncio.wait_for(chain.entered.wait(), 1)
        assert chain.player._lock.locked()
        assert chain.data.play_action_refcount >= 2
        assert chain.players._task_held_locks.get(attempt.start_task)
        if mode == "stop":
            await chain.private("stop")
        await asyncio.wait_for(asyncio.shield(attempt.watchdog), 5.1)
        assert attempt.start_task.cancelled()
        assert chain.stopped.is_set()
        assert chain.player.stream is None
        assert time.monotonic() - dispatch < 5
        chain.assert_drained()
    finally:
        chain.release.set()
        await attempt.request_stop("fixture-end")


async def test_ordinary_http_stop_still_waits_for_held_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the confirmed generic slow-start blocker as a real-monotonic red control."""
    chain = Chain(monkeypatch)
    dispatch = time.monotonic()
    play = asyncio.create_task(
        chain.http(
            "player_queues/play_media",
            {
                "queue_id": PLAYER,
                "media": URL,
                "option": "replace",
            },
        )
    )
    await asyncio.wait_for(chain.entered.wait(), 1)
    stop = asyncio.create_task(chain.http("players/cmd/stop", {"player_id": PLAYER}))
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(stop), max(0, dispatch + 5.05 - time.monotonic()))
        assert not play.done()
        assert not stop.done()
        assert not chain.stopped.is_set()
    finally:
        chain.release.set()
        await asyncio.wait_for(asyncio.gather(play, stop), 3)
        await asyncio.gather(*chain.background)
    chain.assert_drained()


async def test_controller_contention_expires_without_later_queue_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expiry while waiting on the actual controller lock cannot mutate after release."""
    chain = Chain(monkeypatch)
    await chain.private("arm")
    async with chain.players.get_group_and_player_lock(PLAYER, strict=True):
        await chain.private("play")
        attempt = chain.provider._private_transport_attempt
        await asyncio.wait_for(asyncio.shield(attempt.watchdog), 5.1)
        assert attempt.start_task.cancelled()
    assert not chain.entered.is_set()
    assert chain.queue.items == 0
    chain.assert_drained()
