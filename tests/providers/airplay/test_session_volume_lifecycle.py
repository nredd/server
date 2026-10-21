"""Ownership, telemetry and receipt-lifetime regressions for logical session volume."""

from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import PlaybackState
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.providers.airplay import session_volume
from music_assistant.providers.airplay.provider import AirPlayProvider
from music_assistant.providers.airplay.session_volume import SessionVolumeBackend, VolumeRequest
from tests.providers.airplay.dacp.helpers import build_dacp_request, replay
from tests.providers.airplay.test_session_volume import make_rig


@pytest.mark.parametrize("checked", [False, True])
@pytest.mark.parametrize(("muted", "reported"), [(False, -144), (True, -12)])
async def test_dacp_device_volume_mute_telemetry(checked: bool, muted: bool, reported: int) -> None:
    """Real HTTP telemetry cannot mutate mute or write commands in checked mode."""
    rig = make_rig(1)
    try:
        stream = rig.streams[0]
        player = stream.player
        player._attr_volume_muted = muted
        player.update_state()
        stream.session_volume_requested = checked
        cast("MagicMock", player.config.get_value).return_value = False
        provider = MagicMock()
        provider.mass = player.mass
        provider.get_players.return_value = [player]
        provider._handle_dacp_request = AirPlayProvider._handle_dacp_request.__get__(provider)
        rig.commands.clear()
        await replay(
            provider,
            build_dacp_request(
                f"/ctrl-int/1/setproperty?dmcp.device-volume={reported}",
                active_remote=stream.active_remote_id,
            ),
        )
        await asyncio.sleep(0)
        if checked:
            assert player.volume_muted == muted
            assert rig.commands == []
            assert player.volume_level == 60
        else:
            assert player.volume_muted != muted
            assert any(command.startswith("VOLUME=") for command in rig.commands)
    finally:
        await rig.close()


@pytest.mark.parametrize("eof", [False, True])
@pytest.mark.parametrize("paused", [False, True])
async def test_old_stdout_cannot_touch_replacement_backend(eof: bool, paused: bool) -> None:
    """A delayed old read neither handshakes nor closes a replacement session."""
    rig = make_rig(1)
    try:
        stream = rig.streams[0]
        old_backend = stream.session_volume
        old_process = stream._cli_proc
        assert old_process is not None
        reached = asyncio.Event()
        release = asyncio.Event()

        async def read(_size: int) -> bytes:
            reached.set()
            await release.wait()
            return b"" if eof else b"[STATUS] session_volume version=1\n"

        cast("MagicMock", old_process).read = AsyncMock(side_effect=read)
        task = asyncio.create_task(stream._stdout_reader())
        await reached.wait()
        await old_backend.close()
        replacement = SessionVolumeBackend(stream)
        stream.session_volume = replacement
        stream._cli_proc = MagicMock(closed=False, read=AsyncMock(return_value=b""))
        stream.player._attr_playback_state = (
            PlaybackState.PAUSED if paused else PlaybackState.PLAYING
        )
        stream.player.update_state()
        with patch.object(replacement, "close", wraps=replacement.close) as close:
            release.set()
            await task
            close.assert_not_awaited()
        assert not replacement.supported
        assert replacement.generation > old_backend.generation
        assert not stream._cli_proc.closed
        stream._cli_proc.read.assert_not_called()
        assert rig.commands == []
    finally:
        await rig.close()


async def test_checked_commit_reconciles_ordinary_nudge_target() -> None:
    """A checked commit becomes the next nudge base, not an obsolete UI target."""
    rig = make_rig(1)
    try:
        await rig.bind()
        player = rig.streams[0].player
        await rig.controller.cmd_volume_set(player.player_id, 80)
        await rig.request(200)
        assert rig.controller._volume_nudge_base(player) == 20
        await rig.controller.cmd_volume_down(player.player_id)
        assert rig.streams[0].session_volume.confirmed_level == 18
    finally:
        await rig.close()


async def test_checked_commit_preserves_newer_queued_ordinary_target() -> None:
    """A UI intent queued under the member lock remains the next nudge base."""
    rig = make_rig(1)
    try:
        await rig.bind()
        player = rig.streams[0].player
        await rig.controller.cmd_volume_set(player.player_id, 80)
        rig.delay = asyncio.Event()
        rig.reached.clear()
        request = asyncio.create_task(rig.request(200))
        await rig.reached.wait()
        ordinary = asyncio.create_task(rig.controller.cmd_volume_set(player.player_id, 70))
        await asyncio.sleep(0)
        assert rig.controller._volume_nudge_base(player) == 70
        rig.delay.set()
        await request
        await ordinary
        assert rig.controller._volume_nudge_base(player) == 70
        assert rig.streams[0].session_volume.confirmed_level == 70
    finally:
        await rig.close()


@pytest.mark.parametrize("timeout", [False, True])
@pytest.mark.parametrize(("count", "owner"), [(1, 0), (2, 1)])
async def test_ordinary_failure_publishes_unknown_to_binding_owner(
    timeout: bool, count: int, owner: int
) -> None:
    """Failure/partial group/timeout clears the owner's state without extra actuation."""
    rig = make_rig(count)
    try:
        backend = await rig.bind(owner)
        rig.commands.clear()
        rig.fail_member = 0
        if count > 1:
            # The offline rig has no state-update event loop to refresh group aggregation.
            rig.streams[0].player.state.group_volume = 60
        if timeout:

            async def missing_receipt(data: bytes) -> bool:
                if b"ACTION=STREAM_VOLUME_APPLY" in data:
                    rig.commands.append(data.decode())
                    return True
                return await rig.write(0, data)

            cast("MagicMock", rig.streams[0].commands_pipe).write_atomic = AsyncMock(
                side_effect=missing_receipt
            )
        command = (
            rig.controller.set_group_volume(rig.streams[0].player, 80)
            if count > 1
            else rig.controller.cmd_volume_set(rig.streams[0].player.player_id, 80)
        )
        with (
            patch.object(session_volume, "_DEADLINE", 0.01),
            pytest.raises((PlayerCommandFailed, TimeoutError, ExceptionGroup)),
        ):
            await command
        await asyncio.sleep(0)
        if backend._feedback_task is not None:
            await backend._feedback_task
        states = [cmd for cmd in rig.commands if "ACTION=SESSION_VOLUME_STATE" in cmd]
        assert states
        assert "SESSION_VOLUME_KNOWN=0" in states[-1]
        assert f"SESSION_VOLUME_GENERATION={backend.generation}" in states[-1]
        assert sum("ACTION=STREAM_VOLUME_APPLY" in cmd for cmd in rig.commands) == count
        assert not any(cmd.startswith("VOLUME=") for cmd in rig.commands)
    finally:
        await rig.close()


async def test_ordinary_failure_notifies_all_bindings_without_extra_actuation() -> None:
    """A member's lost receipt reaches independently bound leader and follower UIs."""
    rig = make_rig()
    try:
        backends = [await rig.bind(0), await rig.bind(1)]
        rig.commands.clear()
        rig.fail_member = 0
        with pytest.raises(PlayerCommandFailed):
            await rig.controller.cmd_volume_set(rig.streams[0].player.player_id, 80)
        for backend in backends:
            assert backend._feedback_task is not None
            await backend._feedback_task
            assert any(
                f"SESSION_VOLUME_GENERATION={backend.generation}\n" in command
                and "ACTION=SESSION_VOLUME_STATE" in command
                and "SESSION_VOLUME_KNOWN=0" in command
                for command in rig.commands
            )
        assert sum("ACTION=STREAM_VOLUME_APPLY" in cmd for cmd in rig.commands) == 1
    finally:
        await rig.close()


async def test_stale_ordinary_failure_cannot_publish_to_replacement_binding() -> None:
    """An old actuator error cannot change a freshly bound process generation."""
    rig = make_rig(1)
    try:
        stream = rig.streams[0]
        old_backend = await rig.bind()
        rig.delay = asyncio.Event()
        task = asyncio.create_task(rig.controller.cmd_volume_set(stream.player.player_id, 80))
        await rig.reached.wait()
        await old_backend.close()
        stream._cli_proc = MagicMock(closed=False)
        stream.session_volume = SessionVolumeBackend(stream)
        replacement = stream.session_volume
        replacement.handle_stdout("[STATUS] session_volume version=1")
        replacement.confirmed_level = 40
        replacement._receipt_snapshot = replacement.snapshot()
        await rig.bind()
        rig.commands.clear()
        rig.delay.set()
        with pytest.raises((PlayerCommandFailed, asyncio.CancelledError)):
            await task
        await asyncio.sleep(0)
        assert replacement.confirmed_level == 40
        assert replacement.binding is not None
        assert rig.commands == []
    finally:
        await rig.close()


async def test_receiver_feedback_never_sends_a_volume_command() -> None:
    """Exercise the real DACP adoption entrypoints, including already-queued adoption."""
    rig = make_rig(1)
    try:
        backend = await rig.bind()
        player = rig.streams[0].player
        rig.commands.clear()
        player.update_volume_from_device(99)
        await player._adopt_device_volume(99)
        await backend.publish_state()
        assert len(rig.commands) == 1
        assert "SESSION_VOLUME_MILLI=600" in rig.commands[0]
        assert "ACTION=SESSION_VOLUME_STATE" in rig.commands[0]
    finally:
        await rig.close()


async def test_ma_ui_volume_set_uses_actual_receipts_not_legacy_volume() -> None:
    """Ordinary controller entrypoints still use confirmed raw actuators in opt-in mode."""
    rig = make_rig(1)
    try:
        await rig.bind()
        rig.clamp = 61
        await rig.controller.cmd_volume_set(rig.streams[0].player.player_id, 63)
        await rig.streams[0].session_volume.publish_state()
        assert rig.streams[0].player.volume_level == 61
        assert "SESSION_VOLUME_MILLI=610" in rig.commands[-1]
        assert any("ACTION=STREAM_VOLUME_APPLY" in command for command in rig.commands)
        assert not any(command.startswith("VOLUME=") for command in rig.commands)
    finally:
        await rig.close()


async def test_muted_member_stays_zero_in_checked_group_engine() -> None:
    """The checked path preserves the core mute constraint rather than lifting mute."""
    rig = make_rig()
    try:
        player = rig.streams[1].player
        await player.volume_mute(True)
        await rig.streams[0].session_volume.apply(60)
        await rig.streams[1].session_volume.apply(0)
        await rig.bind()
        await rig.request(800)
        assert rig.streams[1].session_volume.confirmed_level == 0
        assert player.volume_muted
        assert rig.streams[0].session_volume.confirmed_level == 80
    finally:
        await rig.close()


async def test_checked_mute_can_restore_previous_raw_volume() -> None:
    """Checked mute commits silence without losing the unmute level."""
    rig = make_rig(1)
    try:
        player = rig.streams[0].player
        await player.volume_mute(True)
        assert rig.streams[0].session_volume.confirmed_level == 0
        assert player.volume_level == 60
        await player.volume_mute(False)
        assert rig.streams[0].session_volume.confirmed_level == 60
        assert not player.volume_muted
    finally:
        await rig.close()


async def test_stale_before_scheduling_allocates_no_worker() -> None:
    """Rejected ownership never schedules a group-volume operation."""
    rig = make_rig(1)
    try:
        backend = await rig.bind()
        rig.streams[0].superseded = True
        backend.request(VolumeRequest(backend.generation, 1, 500))
        assert backend._runner is None
        await backend.publish_state()
        assert backend.binding is None
        assert "ACTION=SESSION_VOLUME_UNBIND" in rig.commands[-1]
        assert not any("STREAM_VOLUME_APPLY" in command for command in rig.commands)
    finally:
        await rig.close()


async def test_queued_actuator_rechecks_owner_after_its_lock() -> None:
    """Even the private actuator lane cannot dispatch work queued before takeover."""
    rig = make_rig(1)
    try:
        backend = await rig.bind()
        async with backend._actuator_lock:
            backend.request(VolumeRequest(backend.generation, 1, 500))
            await asyncio.sleep(0)
            rig.streams[0].superseded = True
        assert backend._runner is not None
        await backend._runner
        assert not any("STREAM_VOLUME_APPLY" in command for command in rig.commands)
    finally:
        await rig.close()


async def test_timeout_and_late_receipt_cannot_initialize_known_state() -> None:
    """A delayed command may have applied, but its late response is never a success."""
    rig = make_rig(1)
    try:
        stream = rig.streams[0]
        backend = stream.session_volume
        stream.commands_pipe = MagicMock(write_atomic=AsyncMock(return_value=True))
        with patch.object(session_volume, "_DEADLINE", 0.01), pytest.raises(TimeoutError):
            await backend.apply(63)
        backend.handle_stdout(
            f"[EVENT] stream_volume_result generation={backend.generation} "
            f"ordinal={backend._ordinal} status=committed level=61"
        )
        assert backend.confirmed_level is None
        assert backend._pending is None
    finally:
        await rig.close()


async def test_failed_receipt_does_not_optimistically_update_player() -> None:
    """A FIFO write and failed actuator response leave no fabricated 80 percent."""
    rig = make_rig(1)
    try:
        rig.fail_member = 0
        player = rig.streams[0].player
        with pytest.raises(PlayerCommandFailed):
            await player.volume_set(80)
        assert player.volume_level == 60
        assert rig.streams[0].session_volume.confirmed_level is None
    finally:
        await rig.close()


async def test_missing_current_owner_rejects_binding() -> None:
    """A missing visible parent cannot be bypassed by native stream fallback."""
    rig = make_rig(1)
    try:
        rig.streams[0].player.set_protocol_parent_id("missing")
        with pytest.raises(PlayerCommandFailed):
            await rig.bind()
        assert not any("SESSION_VOLUME_BIND" in command for command in rig.commands)
    finally:
        await rig.close()


async def test_cyclic_protocol_owner_rejects_binding() -> None:
    """Ownership cycles are unavailable rather than arbitrarily choosing a member."""
    rig = make_rig()
    try:
        rig.streams[0].player.set_protocol_parent_id(rig.streams[1].player.player_id)
        rig.streams[1].player.set_protocol_parent_id(rig.streams[0].player.player_id)
        with pytest.raises(PlayerCommandFailed, match="Cyclic"):
            await rig.bind()
    finally:
        await rig.close()


async def test_membership_change_clears_receipts_even_without_binding() -> None:
    """A receipt is not reusable after regrouping in the same process generation."""
    rig = make_rig()
    try:
        stream = rig.streams[0]
        backend = stream.session_volume
        generation = backend.generation
        assert stream.session is not None
        stream.session.sync_clients.pop()
        backend._state_changed(stream.player, {})
        assert backend.confirmed_level is None
        assert backend.generation != generation
    finally:
        await rig.close()


async def test_disabled_policy_cannot_be_enabled_by_a_handshake() -> None:
    """A capable binary cannot opt the server into experimental volume by itself."""
    rig = make_rig(1)
    try:
        stream = rig.streams[0]
        stream.session_volume_requested = False
        stream.session_volume.supported = False
        stream.session_volume.handle_stdout("[STATUS] session_volume version=1")
        assert not stream.session_volume.enabled
        assert not stream.session_volume.supported
        assert not await stream.session_volume.bind(b"offline", b"", verified_identity=True)
        assert rig.commands == []
    finally:
        await rig.close()
