"""
Separate offline receipt semantics from the real fail-closed transaction budget.

References:
    https://docs.python.org/3/library/asyncio-task.html#timeouts
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from music_assistant.controllers.players.volume_execution import CheckedVolumeExecution
from music_assistant.providers.airplay import session_volume
from music_assistant.providers.airplay.sendspin_bridge import SendspinAirPlayBridge
from tests.providers.airplay.test_session_volume import make_rig
from tests.providers.airplay.test_session_volume_bridge import make_bridge_rig


@pytest.mark.parametrize("bridge", [False, True])
async def test_offline_receipts_survive_scheduler_suspension(bridge: bool) -> None:
    """Balance and actual clamp checks do not depend on physical scheduler latency."""
    rig = (await make_bridge_rig()).rig if bridge else make_rig()
    try:
        await rig.bind()
        rig.clamp = 61
        checked = rig.controller.checked_group_volume

        async def suspended(
            player_id: str, volume_level: int, execution: CheckedVolumeExecution
        ) -> int:
            """Suspend only after the transaction has entered its timeout scope."""
            asyncio.get_running_loop().call_soon(time.sleep, 2.6)
            return await checked(player_id, volume_level, execution)

        with patch.object(rig.controller, "checked_group_volume", side_effect=suspended):
            await rig.request(630)
        assert [stream.session_volume.confirmed_level for stream in rig.streams] == [61, 35]
        completion = next(cmd for cmd in rig.commands if "SESSION_VOLUME_COMPLETE" in cmd)
        assert "SESSION_VOLUME_RESULT=committed" in completion
        assert "SESSION_VOLUME_MILLI=610" in completion
        assert not any(cmd.startswith("VOLUME=") for cmd in rig.commands)
    finally:
        await rig.close()


async def test_rig_timeout_control_is_module_local_and_restored() -> None:
    """Offline control cannot replace asyncio's timeout or escape fixture cleanup."""
    original_asyncio = vars(session_volume)["asyncio"]
    original_timeout = asyncio.timeout
    rig = make_rig(1)
    try:
        scoped_asyncio = vars(session_volume)["asyncio"]
        assert scoped_asyncio is not original_asyncio
        assert asyncio.timeout is original_timeout
        async with scoped_asyncio.timeout(2.5) as scope:
            assert scope.when() is None
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0):
                await asyncio.sleep(0)
    finally:
        await rig.close()
    assert vars(session_volume)["asyncio"] is original_asyncio
    assert asyncio.timeout is original_timeout


async def test_failed_bridge_setup_cannot_leave_timeout_control_installed() -> None:
    """An awaited bridge setup error must leave production deadlines untouched."""
    original_asyncio = vars(session_volume)["asyncio"]
    with (
        patch.object(SendspinAirPlayBridge, "start", new=AsyncMock(side_effect=RuntimeError)),
        pytest.raises(RuntimeError),
    ):
        await make_bridge_rig(1)
    assert vars(session_volume)["asyncio"] is original_asyncio


async def test_real_transaction_deadline_discards_all_receipts() -> None:
    """The unchanged production budget clears the group and rejects late receipts."""
    assert session_volume._DEADLINE == 2.5
    rig = make_rig(2, real_deadlines=True)
    try:
        assert vars(session_volume)["asyncio"] is asyncio
        await rig.bind()
        rig.delay = asyncio.Event()
        await rig.request(630)
        assert rig.reached.is_set()
        assert [stream.session_volume.confirmed_level for stream in rig.streams] == [None, None]
        completion = next(cmd for cmd in rig.commands if "SESSION_VOLUME_COMPLETE" in cmd)
        assert "SESSION_VOLUME_RESULT=unknown" in completion
        assert "SESSION_VOLUME_KNOWN=0" in completion
        assert "SESSION_VOLUME_RESULT=committed" not in completion
        for stream in rig.streams:
            backend = stream.session_volume
            backend.handle_stdout(
                f"[EVENT] stream_volume_result generation={backend.generation} "
                f"ordinal={backend._ordinal} status=committed level=61"
            )
            assert backend.confirmed_level is None
            assert backend._pending is None
    finally:
        await rig.close()
