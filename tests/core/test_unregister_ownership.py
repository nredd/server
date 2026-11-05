"""
Tests for ownership-safe player removal.

References:
- https://github.com/nredd/server/issues/66
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.controllers.players import PlayerController
from music_assistant.mass import MusicAssistant


@pytest.mark.parametrize("pause", ["release", "unload"])
async def test_expected_player_does_not_clean_up_replacement(
    mass_minimal: MusicAssistant, monkeypatch: pytest.MonkeyPatch, pause: str
) -> None:
    """A replacement installed across either teardown await remains untouched."""
    controller = PlayerController(mass_minimal)
    mass_minimal.players = controller
    mass_minimal.player_queues = MagicMock()
    player = MagicMock()
    player.player_id = "test"
    replacement = MagicMock()
    controller._players["test"] = player
    started, proceed = asyncio.Event(), asyncio.Event()

    async def parked(*_args: object) -> None:
        started.set()
        await proceed.wait()

    monkeypatch.setattr(
        controller,
        "_release_audio_source",
        AsyncMock(side_effect=parked if pause == "release" else None),
    )
    player.on_unload = AsyncMock(side_effect=parked if pause == "unload" else None)
    monkeypatch.setattr(controller, "_clear_sleep_timer", MagicMock())
    monkeypatch.setattr(controller, "_schedule_update_all_players", MagicMock())
    signal = MagicMock()
    monkeypatch.setattr(mass_minimal, "signal_event", signal)
    task = asyncio.create_task(controller.unregister("test", expected_player=player))
    await asyncio.wait_for(started.wait(), 2)
    controller._players["test"] = replacement
    proceed.set()
    await task
    assert controller.get_player("test") is replacement
    signal.assert_not_called()
    if pause == "release":
        player.on_unload.assert_not_awaited()
        mass_minimal.player_queues.on_player_remove.assert_not_called()
