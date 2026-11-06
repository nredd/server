"""
Actual session publication and START-lock revocation boundaries.

References:
    https://docs.python.org/3/library/asyncio-sync.html
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from music_assistant.helpers.private_transport import CURRENT_ATTEMPT, PrivateTransportAttempt
from music_assistant.providers.airplay.stream import AirPlayStream
from tests.providers.airplay.test_stream import _make_cli_proc, _make_player
from tests.providers.airplay.test_stream_session import _make_session


@pytest.mark.parametrize("window", ["spawn-lock", "connect", "foreign"])
async def test_real_publication_boundary_revocation(
    monkeypatch: pytest.MonkeyPatch,
    window: str,
) -> None:
    """Revocation cannot publish late or feed a displaced/newer stream."""
    player = _make_player()
    player.stream_spawn_lock = asyncio.Lock()
    player.get_stream_pcm_format.return_value = None
    attempt = PrivateTransportAttempt("a" * 32, player, object(), "unused", lambda: True)
    session = _make_session(0, 0)
    session.private_attempt = attempt
    session.sync_clients = [player]
    feed = AsyncMock()
    monkeypatch.setattr(session, "_start_player_ffmpeg", feed)
    entered, release = asyncio.Event(), asyncio.Event()

    async def connect(_stream: AirPlayStream, _shared: bool) -> None:
        """Hold only receiver connection I/O, not the actual publication code."""
        entered.set()
        await release.wait()

    monkeypatch.setattr(AirPlayStream, "connect", connect)
    attempt.bind_session(session, session.private_release)
    if window == "spawn-lock":
        await player.stream_spawn_lock.acquire()
    token = CURRENT_ATTEMPT.set(attempt)
    try:
        attempt.play(lambda: session._start_client(player, False))
    finally:
        CURRENT_ATTEMPT.reset(token)
    foreign: Any = None
    try:
        if window == "spawn-lock":
            await asyncio.sleep(0)
        else:
            await entered.wait()
            assert player.stream is attempt.stream
            if window == "foreign":
                foreign = object()
                player.stream = foreign
        await attempt.request_stop("stop")
        release.set()
        if window == "spawn-lock":
            player.stream_spawn_lock.release()
        await attempt.watchdog
        assert attempt.start_task is not None
        assert attempt.start_task.cancelled()
        feed.assert_not_awaited()
        assert player.stream is foreign
    finally:
        release.set()
        if player.stream_spawn_lock.locked():
            player.stream_spawn_lock.release()
        await attempt.request_stop("fixture-end")


async def test_private_teardown_bypasses_ordinary_locks_without_volume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EOF may park a CLI, so send only captured transport STOP without metadata locks."""
    player = _make_player()
    stream = AirPlayStream(player)
    player.stream = stream
    process = SimpleNamespace(proc=SimpleNamespace(returncode=None), _stderr_reader_task=None)
    monkeypatch.setattr(stream, "_cli_proc", process)
    writer, remove = AsyncMock(return_value=True), AsyncMock()
    monkeypatch.setattr(stream, "_write_cli_command", writer)
    monkeypatch.setattr(stream.commands_pipe, "remove", remove)
    async with stream._metadata_lock, stream._stop_lock:
        await asyncio.wait_for(stream.private_release(), 0.5)
        assert stream._metadata_lock.locked()
        assert stream._stop_lock.locked()
    writer.assert_awaited_once_with("ACTION=STOP")
    remove.assert_awaited_once()
    assert player.stream is None


async def test_real_start_metadata_lock_has_no_late_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real START method rechecks revocation after acquiring its metadata lock."""
    player = _make_player()
    stream = AirPlayStream(player)
    player.stream = stream
    stream._cli_proc = _make_cli_proc()
    stream._connected.set()
    writer = AsyncMock(return_value=True)
    monkeypatch.setattr(stream, "_write_cli_command", writer)
    attempt = PrivateTransportAttempt("a" * 32, player, object(), "unused", lambda: True)
    stream.private_attempt = attempt
    attempt.bind_stream(stream)
    await stream._metadata_lock.acquire()
    start = asyncio.create_task(stream.start())
    try:
        await stream._start_waiting.wait()
        await attempt.request_stop("stop")
        stream._metadata_lock.release()
        with pytest.raises(asyncio.CancelledError):
            await start
        writer.assert_not_awaited()
    finally:
        if stream._metadata_lock.locked():
            stream._metadata_lock.release()
        await asyncio.gather(start, return_exceptions=True)
        await attempt.watchdog


async def test_real_fifo_start_guard_refuses_revocation_during_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use the actual START callback handoff, not a pre-lock permission assertion."""
    player = _make_player()
    stream = AirPlayStream(player)
    player.stream = stream
    stream._cli_proc = _make_cli_proc()
    stream._connected.set()
    attempt = PrivateTransportAttempt("a" * 32, player, object(), "unused", lambda: True)
    stream.private_attempt = attempt
    attempt.bind_stream(stream)
    calls: list[str] = []

    async def write(command: str, **kwargs: Any) -> bool:
        """Revoke at the writer boundary before its repeated guard call."""
        attempt.request_stop("delivery-revoked")
        kwargs["guard"]()
        calls.append(command)
        return True

    monkeypatch.setattr(stream, "_write_cli_command", write)
    with pytest.raises(asyncio.CancelledError):
        await stream.start()
    assert calls == []
    await attempt.request_stop("fixture-end")
    await attempt.watchdog
