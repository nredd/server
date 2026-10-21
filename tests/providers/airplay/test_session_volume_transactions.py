"""
Regressions for committed mute state and transactional logical-volume binding.

References:
    - music_assistant/providers/airplay/session_volume.py
    - music_assistant/providers/airplay/player.py
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.providers.airplay.constants import CONF_STORED_VOLUME
from music_assistant.providers.airplay.session_volume import SessionVolumeBackend
from tests.providers.airplay.test_session_volume import make_rig


def _assert_retired(backend: SessionVolumeBackend, generation: int) -> None:
    """Require uncertain registration to lose its binding and generation authority."""
    assert backend.binding is None
    assert backend.generation > generation
    assert backend not in backend._dependents


async def test_checked_unmute_adopts_and_persists_accepted_level() -> None:
    """Muting preserves the restore target; unmuting adopts the actual receipt."""
    rig = make_rig(1)
    try:
        player = rig.streams[0].player
        await player.volume_mute(True)
        assert player.volume_level == 60
        rig.clamp = 40
        with patch.object(rig.controller.mass.config, "set_raw_player_config_value") as persist:
            await player.volume_mute(False)
        assert not player.volume_muted
        assert player.volume_level == 40
        assert rig.streams[0].session_volume.confirmed_level == 40
        persist.assert_called_once_with(player.player_id, CONF_STORED_VOLUME, 40)
    finally:
        await rig.close()


@pytest.mark.parametrize("failure", [asyncio.CancelledError, RuntimeError])
async def test_failed_bind_fences_generation_and_can_retry(
    failure: type[BaseException],
) -> None:
    """An uncertain bind cannot survive or reuse its generation on a retry."""
    rig = make_rig(1)
    backend = rig.streams[0].session_volume
    generation = backend.generation
    try:
        with (
            patch.object(
                rig.streams[0],
                "send_cli_command",
                new=AsyncMock(side_effect=[failure(), True]),
            ),
            pytest.raises(failure),
        ):
            await backend.bind(b"offline-output", b"", verified_identity=True)
        _assert_retired(backend, generation)
        assert await backend.bind(b"retry-output", b"", verified_identity=True)
        assert backend.binding is not None
        assert backend.binding.generation == backend.generation
        assert backend.binding.output_uid == b"retry-output"
    finally:
        await rig.close()


async def test_bind_revalidates_owner_after_delivery() -> None:
    """An owner lost while binding cannot install dependencies or return success."""
    rig = make_rig(1)
    stream = rig.streams[0]
    backend = stream.session_volume
    reached = asyncio.Event()
    release = asyncio.Event()

    async def delayed_write(_command: str) -> bool:
        """Pause the real bind await boundary without performing any actuator work."""
        reached.set()
        await release.wait()
        return True

    try:
        with patch.object(stream, "send_cli_command", side_effect=delayed_write):
            task = asyncio.create_task(backend.bind(b"offline-output", b"", verified_identity=True))
            await asyncio.wait_for(reached.wait(), 1)
            stream.superseded = True
            release.set()
            with pytest.raises(PlayerCommandFailed):
                await task
        assert backend.binding is None
        assert backend not in backend._dependents
        assert not any("STREAM_VOLUME_APPLY" in command for command in rig.commands)
    finally:
        release.set()
        await rig.close()
