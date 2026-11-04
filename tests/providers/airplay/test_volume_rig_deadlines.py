"""
Separate offline receipt semantics from the real fail-closed transaction budget.

References:
    https://docs.python.org/3/library/asyncio-task.html#timeouts
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from music_assistant.controllers.players.volume_execution import CheckedVolumeExecution
from music_assistant.helpers.named_pipe import AsyncNamedPipeWriter
from music_assistant.providers.airplay import session_volume
from music_assistant.providers.airplay.sendspin_bridge import SendspinAirPlayBridge
from tests.providers.airplay import test_session_volume as volume_tests
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


_FIFO_TESTS: dict[str, Callable[[Path, pytest.MonkeyPatch], Awaitable[None]]] = {
    "retired_snapshot": lambda path, mp: (
        volume_tests.test_retired_pending_snapshot_cannot_poison_successor_transaction(
            path, mp, "invalidate"
        )
    ),
    "membership_change": volume_tests.test_unknown_startup_state_retires_queued_transaction_on_membership_change,
    "current_delivery": volume_tests.test_current_checked_transaction_delivers_to_real_fifo,
    "owner_change": volume_tests.test_owner_change_drops_ordinary_checked_transaction_queued_on_real_fifo,
}


@pytest.mark.parametrize("name", sorted(_FIFO_TESTS))
async def test_fifo_setup_failure_restores_real_deadlines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Failing after the offline patch but before the test body cannot leak the patch."""
    original = vars(session_volume)["asyncio"]
    with (
        patch.object(AsyncNamedPipeWriter, "create", side_effect=OSError("injected")),
        pytest.raises(OSError, match="injected"),
    ):
        await _FIFO_TESTS[name](tmp_path, monkeypatch)
    assert vars(session_volume)["asyncio"] is original
    assert not (tmp_path / "commands").exists()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0):
            await asyncio.sleep(0)
    rig = make_rig(1, real_deadlines=True)
    try:
        assert vars(session_volume)["asyncio"] is original
        with pytest.raises(TimeoutError):
            async with vars(session_volume)["asyncio"].timeout(0):
                await asyncio.sleep(0)
    finally:
        await rig.close()


@pytest.mark.parametrize("open_fails", [False, True])
async def test_partial_fifo_setup_is_released_by_close(tmp_path: Path, open_fails: bool) -> None:
    """A failure after the FIFO exists, or after the reader opens, still frees both."""
    original = vars(session_volume)["asyncio"]
    descriptors: list[int] = []
    real_open = os.open

    def tracking_open(path: str, flags: int) -> int:
        """Record the reader so the test can prove it was closed."""
        descriptors.append(real_open(path, flags))
        return descriptors[-1]

    rig = make_rig(1)
    try:
        with patch.object(
            os, "open", side_effect=OSError("injected") if open_fails else tracking_open
        ):
            if open_fails:
                with pytest.raises(OSError, match="injected"):
                    await rig.open_fifo(tmp_path)
            else:
                await rig.open_fifo(tmp_path)
                assert (tmp_path / "commands").exists()
    finally:
        await rig.close()
    assert vars(session_volume)["asyncio"] is original
    assert not (tmp_path / "commands").exists()
    for descriptor in descriptors:
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(descriptor)
