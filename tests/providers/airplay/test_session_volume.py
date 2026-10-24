"""Offline tests of logical volume through real controller and CLI transaction seams."""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Self, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import PlayerFeature, PlayerType
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.constants import CONF_MAX_VOLUME, CONF_MIN_VOLUME, CONF_VOLUME_CONTROL
from music_assistant.controllers.players import PlayerController
from music_assistant.controllers.players.constants import PlayerLockPurpose
from music_assistant.helpers.named_pipe import AsyncNamedPipeWriter
from music_assistant.helpers.process import AsyncProcess
from music_assistant.providers.airplay.constants import (
    CONF_EXPERIMENTAL_HOMEPOD_REMOTE,
    CONF_EXPERIMENTAL_SESSION_VOLUME,
)
from music_assistant.providers.airplay.player import AirPlayPlayer
from music_assistant.providers.airplay.provider import AirPlayProvider
from music_assistant.providers.airplay.session_volume import (
    SessionVolumeBackend,
    VolumeRequest,
    parse_receipt,
    parse_request,
)
from music_assistant.providers.airplay.stream import AirPlayStream
from music_assistant.providers.airplay.stream_session import AirPlayStreamSession
from tests.common import MockPlayer, MockProvider
from tests.providers.airplay.test_homepod_remote_admission import _consume_task
from tests.providers.airplay.test_stream import _make_player


class ObservedLock:
    """Expose when a second task is waiting on the private actuator lock."""

    def __init__(self) -> None:
        """Create an unlocked gate and its deterministic waiting signal."""
        self._lock = asyncio.Lock()
        self.waiting = asyncio.Event()

    async def __aenter__(self) -> Self:
        """Acquire the gate, recording contention before waiting."""
        if self._lock.locked():
            self.waiting.set()
        await self._lock.acquire()
        return self

    async def __aexit__(self, *_args: object) -> None:
        """Release the gate after the protected operation settles."""
        self._lock.release()


@dataclass
class VolumeRig:
    """Real controller/players with explicitly synthetic, receipt-producing device seams."""

    controller: PlayerController
    streams: list[AirPlayStream]
    commands: list[str] = field(default_factory=list)
    clamp: int = 100
    fail_member: int | None = None
    delay: asyncio.Event | None = None
    reached: asyncio.Event = field(default_factory=asyncio.Event)

    async def write(
        self, index: int, data: bytes, *, guard: Callable[[], bool] | None = None
    ) -> bool:
        """Act as a CLI pipe reader, accepting only current checked transactions."""
        if guard is not None and not guard():
            return False
        command = data.decode()
        self.commands.append(command)
        if "ACTION=STREAM_VOLUME_APPLY" not in command:
            return True
        fields = dict(line.split("=", 1) for line in command.strip().splitlines())
        self.reached.set()
        if self.delay is not None:
            await self.delay.wait()
        status = "failed" if self.fail_member == index else "committed"
        level = min(int(fields["STREAM_VOLUME_LEVEL"]), self.clamp)
        self.streams[index].session_volume.handle_stdout(
            f"[EVENT] stream_volume_result generation={fields['STREAM_VOLUME_GENERATION']} "
            f"ordinal={fields['STREAM_VOLUME_ORDINAL']} status={status}"
            + (f" level={level}" if status == "committed" else "")
        )
        return True

    async def bind(self, index: int = 0) -> SessionVolumeBackend:
        """Use synthetic identity ONLY for the offline adapter contract."""
        backend = self.streams[index].session_volume
        assert await backend.bind(b"offline-output", b"", verified_identity=True)
        return backend

    async def request(self, milli: int, ordinal: int = 1, index: int = 0) -> None:
        """Submit a native-style request and await its bounded worker."""
        backend = self.streams[index].session_volume
        backend.request(VolumeRequest(backend.generation, ordinal, milli))
        assert backend._runner is not None
        await backend._runner

    async def close(self) -> None:
        """Detach callbacks and settle locally scheduled feedback."""
        for stream in self.streams:
            await stream.session_volume.close()
        await asyncio.sleep(0)


def make_rig(count: int = 2) -> VolumeRig:
    """Build a real native sync group without starting playback or contacting devices."""
    mass = MagicMock()
    mass.closing = False
    mass.config.get_raw_core_config_value.return_value = "GLOBAL"
    mass.create_task.side_effect = asyncio.create_task
    mass.config.get_raw_player_config_value.side_effect = lambda _id, _key, default=None: default
    mass.config.get_player_config_value.side_effect = lambda _id, _key, default=None: default
    mass.player_queues.get.return_value = None
    controller = PlayerController(mass)
    controller.config = MagicMock()
    controller.config.get_value.side_effect = lambda _key, default=None: default
    mass.players = controller
    provider = MagicMock()
    provider.mass = mass
    provider.domain = "airplay"
    provider.instance_id = "airplay-test"
    provider.unloading = False
    provider.bridge_manager.get_volume_command_context.return_value = None
    players = [
        AirPlayPlayer(
            provider=cast("AirPlayProvider", provider),
            player_id=f"aabbccdde00{index}",
            display_name=f"Speaker {index}",
            address="127.0.0.1",
            manufacturer="Test",
            model="Test",
            raop_discovery_info=None,
            airplay_discovery_info=None,
        )
        for index in range(count)
    ]
    controller._players = {player.player_id: player for player in players}
    provider.players = players
    for index, player in enumerate(players):
        player._attr_available = True
        player._attr_powered = True
        player._attr_volume_level = 60 if index == 0 else 30
        if count > 1:
            if index == 0:
                player._attr_group_members = [member.player_id for member in players]
        player.set_initialized()
    for player in players:
        player.update_state(force_update=True, signal_event=False)
    session = cast("AirPlayStreamSession", MagicMock(sync_clients=players))
    streams = [AirPlayStream(player) for player in players]
    rig = VolumeRig(controller, streams)
    for index, stream in enumerate(streams):
        stream.player.stream = stream
        stream.session = session
        stream._cli_proc = MagicMock(closed=False)
        stream.session_volume_requested = True
        stream.session_volume.handle_stdout("[STATUS] session_volume version=1")
        stream.session_volume.confirmed_level = 60 if index == 0 else 30
        stream.commands_pipe = MagicMock(
            write=AsyncMock(side_effect=partial(rig.write, index)),
            write_atomic=AsyncMock(side_effect=partial(rig.write, index)),
        )
    for stream in streams:
        stream.session_volume._receipt_snapshot = stream.session_volume.snapshot()
    return rig


@pytest.mark.parametrize(
    "line",
    [
        "[EVENT] session_volume_set generation=0 ordinal=1 milli=630",
        "[EVENT] session_volume_set generation=1 ordinal=0 milli=630",
        "[EVENT] session_volume_set generation=18446744073709551616 ordinal=1 milli=630",
        "[EVENT] session_volume_set generation=1 ordinal=1 milli=1001",
        "[EVENT] session_volume_set generation=1 ordinal=1 milli=-1",
        "[EVENT] session_volume_set generation=1 ordinal=1 milli=nan",
        "[EVENT] session_volume_set generation=1 ordinal=1 milli=630 milli=630",
        "[EVENT] session_volume_set generation=1 ordinal=1 milli=630 extra=1",
    ],
)
def test_strict_request_parser(line: str) -> None:
    """Malformed, duplicate, overflow and nonfinite fields cannot be reused."""
    assert parse_request(line) is None
    assert parse_request(
        "[EVENT] session_volume_set generation=7 ordinal=12 milli=630"
    ) == VolumeRequest(7, 12, 630)


@pytest.mark.parametrize(
    "suffix",
    [
        "status=committed",
        "status=failed level=0",
        "status=committed level=101",
        "status=committed level=0 level=0",
        "status=unknown extra=1",
    ],
)
def test_strict_receipt_parser(suffix: str) -> None:
    """Only a committed receipt carries a bounded raw device level."""
    assert parse_receipt(f"[EVENT] stream_volume_result generation=7 ordinal=3 {suffix}") is None


async def test_real_controller_balance_and_actual_clamp() -> None:
    """Interpolate unequal speakers, then return the actual maximum, not desired 630."""
    rig = make_rig()
    try:
        await rig.bind()
        rig.clamp = 61
        await rig.request(630)
        assert rig.streams[0].session_volume.confirmed_level == 61
        assert rig.streams[1].session_volume.confirmed_level == 35
        completion = rig.commands[-1]
        assert "SESSION_VOLUME_RESULT=committed" in completion
        assert "SESSION_VOLUME_MILLI=610" in completion
        assert not any(command.startswith("VOLUME=") for command in rig.commands)
    finally:
        await rig.close()


async def test_unknown_is_not_zero_and_cannot_initialize_from_persisted_state() -> None:
    """Stored 60 is not authoritative; an explicit accepted zero is."""
    rig = make_rig(1)
    try:
        backend = rig.streams[0].session_volume
        backend.confirmed_level = None
        await rig.bind()
        assert "SESSION_VOLUME_KNOWN=0" in rig.commands[-1]
        await rig.request(630)
        assert "SESSION_VOLUME_RESULT=rejected" in rig.commands[-1]
        assert not any("STREAM_VOLUME_APPLY" in command for command in rig.commands)
        await backend.apply(0)
        await backend.publish_state()
        assert "SESSION_VOLUME_KNOWN=1\nSESSION_VOLUME_MILLI=0" in rig.commands[-1]
    finally:
        await rig.close()


async def test_follower_resolves_exact_leader_not_first_session_client() -> None:
    """Session list order is not volume authority."""
    rig = make_rig()
    try:
        session = rig.streams[0].session
        assert session is not None
        session.sync_clients.reverse()
        backend = await rig.bind(1)
        assert backend.binding is not None
        assert backend.binding.snapshot.owner_id == "aabbccdde000"
        await rig.request(200, index=1)
        assert [stream.session_volume.confirmed_level for stream in rig.streams] == [20, 10]
    finally:
        await rig.close()


async def test_bridge_owner_never_falls_back_to_native() -> None:
    """An unresolved/unconfirmed Sendspin bridge is explicitly unavailable."""
    rig = make_rig(1)
    try:
        rig.streams[0].session = None
        with pytest.raises(PlayerCommandFailed, match="owner"):
            await rig.bind()
        assert rig.commands == []
    finally:
        await rig.close()


async def test_preflight_all_members_prevents_partial_mixed_send() -> None:
    """A mixed unconfirmed member makes the whole operation unavailable before sends."""
    rig = make_rig()
    try:
        rig.streams[1].session_volume.supported = False
        with pytest.raises(PlayerCommandFailed):
            await rig.bind()
        assert rig.commands == []
    finally:
        await rig.close()


async def test_partial_failure_clears_group_state_without_rollback() -> None:
    """One accepted and one failed command produces unknown, not fictional rollback."""
    rig = make_rig()
    try:
        await rig.bind()
        rig.fail_member = 1
        await rig.request(800)
        completion = next(cmd for cmd in rig.commands if "ACTION=SESSION_VOLUME_COMPLETE" in cmd)
        assert "SESSION_VOLUME_RESULT=unknown" in completion
        assert "SESSION_VOLUME_KNOWN=0" in completion
        assert "SESSION_VOLUME_KNOWN=0" in rig.commands[-1]
        assert all(stream.session_volume.confirmed_level is None for stream in rig.streams)
        assert sum("STREAM_VOLUME_APPLY" in command for command in rig.commands) == 2
    finally:
        await rig.close()


@pytest.mark.parametrize("purpose", [PlayerLockPurpose.GROUP_VOLUME, PlayerLockPurpose.VOLUME])
async def test_owner_changed_while_waiting_for_real_lock(purpose: PlayerLockPurpose) -> None:
    """Topology is revalidated after acquiring each real controller lock."""
    rig = make_rig()
    try:
        await rig.bind()
        lock = rig.controller.get_player_lock("aabbccdde000", purpose)
        async with lock:
            backend = rig.streams[0].session_volume
            backend.request(VolumeRequest(backend.generation, 1, 800))
            await asyncio.sleep(0)
            rig.streams[0].superseded = True
        assert backend._runner is not None
        await backend._runner
        assert not any("STREAM_VOLUME_APPLY" in command for command in rig.commands)
        assert backend.binding is None
    finally:
        await rig.close()


async def test_late_completion_after_takeover_is_never_success() -> None:
    """A receipt cannot revive a stale stream even if the device accepted the command."""
    rig = make_rig(1)
    try:
        await rig.bind()
        rig.delay = asyncio.Event()
        backend = rig.streams[0].session_volume
        backend.request(VolumeRequest(backend.generation, 1, 800))
        await rig.reached.wait()
        rig.streams[0].superseded = True
        rig.delay.set()
        assert backend._runner is not None
        await backend._runner
        assert not any("SESSION_VOLUME_RESULT=committed" in command for command in rig.commands)
    finally:
        await rig.close()


async def test_burst_keeps_only_one_executing_and_one_trailing() -> None:
    """A thousand requests cannot allocate a thousand controller tasks or commands."""
    rig = make_rig(1)
    try:
        await rig.bind()
        rig.delay = asyncio.Event()
        backend = rig.streams[0].session_volume
        backend.request(VolumeRequest(backend.generation, 1, 100))
        await rig.reached.wait()
        runner = backend._runner
        for ordinal in range(2, 1002):
            backend.request(VolumeRequest(backend.generation, ordinal, 500))
            assert backend._runner is runner
        rig.delay.set()
        assert runner is not None
        await runner
        assert sum("STREAM_VOLUME_APPLY" in command for command in rig.commands) == 2
        assert any("SESSION_VOLUME_ORDINAL=1001" in command for command in rig.commands)
    finally:
        await rig.close()


async def test_feedback_and_receiver_events_cannot_command_volume() -> None:
    """Publishing known state and MRP/dvlc feedback performs no actuator calls."""
    rig = make_rig(1)
    try:
        backend = await rig.bind()
        rig.commands.clear()
        backend.handle_stdout("[EVENT] dvlc volume=99")
        backend.handle_stdout("[EVENT] mrp volume=99")
        await backend.publish_state()
        assert len(rig.commands) == 1
        assert "ACTION=SESSION_VOLUME_STATE" in rig.commands[0]
        assert "SESSION_VOLUME_MILLI=600" in rig.commands[0]
    finally:
        await rig.close()


async def test_unverified_identity_never_binds_or_publishes() -> None:
    """Operational backend readiness is not permission to publish native UI state."""
    rig = make_rig(1)
    try:
        assert not await rig.streams[0].session_volume.bind(b"synthetic", b"")
        assert rig.commands == []
    finally:
        await rig.close()


async def test_scaled_solo_uses_normal_controller_branch() -> None:
    """Bounds are applied exactly once, and committed raw 61 is inverse-scaled."""
    rig = make_rig(1)
    try:
        cast("MagicMock", rig.controller.mass).config.get_raw_player_config_value.side_effect = (
            lambda _id, key, default=None: {CONF_MIN_VOLUME: 10, CONF_MAX_VOLUME: 90}.get(
                key, default
            )
        )
        await rig.streams[0].session_volume.apply(60)
        await rig.bind()
        await rig.request(630)
        assert rig.streams[0].session_volume.confirmed_level == 60
        assert "SESSION_VOLUME_MILLI=620" in rig.commands[-1]
    finally:
        await rig.close()


async def test_active_group_is_volume_owner() -> None:
    """An explicit active group supersedes the underlying native sync owner."""
    rig = make_rig()
    try:
        mass = cast("MagicMock", rig.controller.mass)
        group = MockPlayer(
            MockProvider("group", mass=mass), "active-group", "Group", PlayerType.GROUP
        )
        group._attr_available = True
        group._attr_powered = True
        group._attr_group_members = [stream.player.player_id for stream in rig.streams]
        rig.controller._players[group.player_id] = group
        group.set_initialized()
        group.update_state(force_update=True, signal_event=False)
        for stream in rig.streams:
            stream.player.update_state(force_update=True, signal_event=False)
        group.update_state(force_update=True, signal_event=False)
        await asyncio.gather(
            *(
                stream.session_volume.apply(60 if index == 0 else 30)
                for index, stream in enumerate(rig.streams)
            )
        )
        backend = await rig.bind()
        assert backend.binding is not None
        assert backend.binding.snapshot.owner_id == group.player_id
        await rig.request(200)
        assert [stream.session_volume.confirmed_level for stream in rig.streams] == [20, 10]
    finally:
        await rig.close()


async def test_visible_protocol_parent_owns_bounds_exactly_once() -> None:
    """Resolve the visible parent and route its already-scaled raw output directly."""
    rig = make_rig(1)
    try:
        mass = cast("MagicMock", rig.controller.mass)
        output = rig.streams[0].player
        parent = MockPlayer(MockProvider("native", mass=mass), "visible", "Visible")
        parent._attr_supported_features = {PlayerFeature.VOLUME_SET}
        parent._attr_available = True
        parent._attr_powered = True
        parent._attr_volume_level = 60
        rig.controller._players[parent.player_id] = parent
        mass.config.get_raw_player_config_value.side_effect = lambda player_id, key, default=None: (
            {CONF_VOLUME_CONTROL: output.player_id, CONF_MIN_VOLUME: 10, CONF_MAX_VOLUME: 90}.get(
                key, default
            )
            if player_id == parent.player_id
            else default
        )
        parent.set_initialized()
        output.set_protocol_parent_id(parent.player_id)
        parent.update_state(force_update=True, signal_event=False)
        output.update_state(force_update=True, signal_event=False)
        await rig.streams[0].session_volume.apply(60)
        backend = await rig.bind()
        assert backend.binding is not None
        assert backend.binding.snapshot.owner_id == parent.player_id
        await rig.request(630)
        assert backend.confirmed_level == 60
        assert any("SESSION_VOLUME_MILLI=620" in command for command in rig.commands)
    finally:
        await rig.close()


@pytest.mark.parametrize(
    "marker",
    ["", "[STATUS] session_volume version=2", "[STATUS] session_volume version=1 version=1"],
)
async def test_older_or_malformed_handshake_keeps_legacy_playback(marker: str) -> None:
    """No new IPC or checked-success claims when handshake support is absent."""
    rig = make_rig(1)
    try:
        stream = rig.streams[0]
        stream.session_volume.supported = False
        stream.session_volume.handle_stdout(marker)
        assert not stream.session_volume.enabled
        assert not await stream.session_volume.bind(b"offline", b"", verified_identity=True)
        await stream._send_current_volume()
        assert rig.commands == ["VOLUME=60\n"]
    finally:
        await rig.close()


@pytest.mark.parametrize(
    ("admission", "volume"), [(False, False), (False, True), (True, False), (True, True)]
)
async def test_child_environment_requires_both_options(
    admission: bool, volume: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only explicit booleans enable the child; ambient environment and parent stay unchanged."""
    monkeypatch.setenv("CLIAIRPLAY_SESSION_VOLUME", "mrp130")
    original = dict(os.environ)
    player = _make_player()
    player.provider.mass.create_task.side_effect = _consume_task
    player.provider.config.get_value.side_effect = lambda key, default=None: {
        CONF_EXPERIMENTAL_HOMEPOD_REMOTE: admission,
        CONF_EXPERIMENTAL_SESSION_VOLUME: volume,
    }.get(key, default)
    stream = AirPlayStream(player)
    stream.commands_pipe = AsyncNamedPipeWriter(str(tmp_path / "commands"))
    with (
        patch.object(stream, "_build_cli_args", new=AsyncMock(return_value=["binary"])),
        patch.object(AsyncProcess, "start", new_callable=AsyncMock),
    ):
        try:
            await stream.connect()
            assert stream._cli_proc is not None
            assert stream._cli_proc._env["CLIAIRPLAY_SESSION_VOLUME"] == (
                "mrp130" if admission and volume else "off"
            )
            assert dict(os.environ) == original
        finally:
            await stream.commands_pipe.remove()
            await stream.session_volume.close()


async def test_fifo_delivery_is_not_actuator_acceptance() -> None:
    """Writing the command cannot initialize known state without a matching receipt."""
    rig = make_rig(1)
    try:
        stream = rig.streams[0]
        stream.session_volume.confirmed_level = None
        stream.commands_pipe = MagicMock(
            write=AsyncMock(return_value=True), write_atomic=AsyncMock(return_value=True)
        )
        task = asyncio.create_task(stream.session_volume.apply(63))
        await asyncio.sleep(0)
        assert not task.done()
        assert stream.session_volume.confirmed_level is None
        ordinal = stream.session_volume._ordinal
        stream.session_volume.handle_stdout(
            f"[EVENT] stream_volume_result generation={stream.session_volume.generation} "
            f"ordinal={ordinal + 1} status=committed level=63"
        )
        assert not task.done()
        stream.session_volume.handle_stdout(
            f"[EVENT] stream_volume_result generation={stream.session_volume.generation} "
            f"ordinal={ordinal} status=committed level=61"
        )
        assert await task == 61
    finally:
        await rig.close()


@pytest.mark.parametrize("retirement", ["owner_changed", "invalidate"])
async def test_retired_pending_snapshot_cannot_poison_successor_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retirement: str
) -> None:
    """A stale event cannot retire the successor waiting behind an old FIFO write."""
    rig = make_rig(1)
    writer = AsyncNamedPipeWriter(str(tmp_path / "commands"))
    await writer.create()
    reader = os.open(writer.path, os.O_RDONLY | os.O_NONBLOCK)
    stream = rig.streams[0]
    backend = stream.session_volume
    lock = ObservedLock()
    monkeypatch.setattr(backend, "_actuator_lock", lock)
    stream.commands_pipe = writer
    reached = asyncio.Event()
    release = asyncio.Event()
    write_atomic = writer.write_atomic
    writes = 0

    async def write_with_delayed_old_transaction(
        data: bytes, *, guard: Callable[[], bool] | None = None
    ) -> bool:
        nonlocal writes
        writes += 1
        if writes == 1:
            reached.set()
            await release.wait()
        delivered = await write_atomic(data, guard=guard)
        if delivered:
            fields = dict(
                line.split("=", 1) for line in os.read(reader, 4096).decode().splitlines()
            )
            backend.handle_stdout(
                f"[EVENT] stream_volume_result generation={fields['STREAM_VOLUME_GENERATION']} "
                f"ordinal={fields['STREAM_VOLUME_ORDINAL']} status=committed level=80"
            )
        return delivered

    monkeypatch.setattr(writer, "write_atomic", write_with_delayed_old_transaction)
    try:
        old = asyncio.create_task(backend.apply(80))
        await asyncio.wait_for(reached.wait(), 1)
        if retirement == "owner_changed":
            backend.owner_changed()
        else:
            await backend.invalidate()
        successor = asyncio.create_task(backend.apply(80))
        await asyncio.wait_for(lock.waiting.wait(), 1)
        backend._state_changed(stream.player, {"volume_level": (60, 61)})
        release.set()
        with pytest.raises(PlayerCommandFailed, match="delivery failed"):
            await old
        assert await successor == 80
        assert (backend._pending, backend._pending_snapshot) == (None, None)
    finally:
        for task_name in ("old", "successor"):
            task = locals().get(task_name)
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        os.close(reader)
        await writer.remove()
        await rig.close()


async def test_unknown_startup_state_retires_queued_transaction_on_membership_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown startup state still binds a queued checked write to its membership."""
    rig = make_rig()
    writer = AsyncNamedPipeWriter(str(tmp_path / "commands"))
    await writer.create()
    reader = os.open(writer.path, os.O_RDONLY | os.O_NONBLOCK)
    stream = rig.streams[0]
    backend = stream.session_volume
    backend._discard_receipt()
    stream.commands_pipe = writer
    queued = asyncio.Event()
    delivered = asyncio.Event()
    send = stream.send_cli_command
    write_atomic = writer.write_atomic

    async def send_after_queue(command: str, *, guard: Callable[[], bool] | None = None) -> bool:
        queued.set()
        return await send(command, guard=guard)

    async def record_delivery(data: bytes, *, guard: Callable[[], bool] | None = None) -> bool:
        result = await write_atomic(data, guard=guard)
        delivered.set()
        return result

    monkeypatch.setattr(stream, "send_cli_command", send_after_queue)
    monkeypatch.setattr(writer, "write_atomic", record_delivery)
    try:
        async with writer._write_lock:
            transaction = asyncio.create_task(backend.apply(80))
            await asyncio.wait_for(queued.wait(), 1)
            assert stream.session is not None
            stream.session.sync_clients.pop()
            backend._state_changed(stream.player, {})
        await asyncio.wait_for(delivered.wait(), 1)
        with pytest.raises(PlayerCommandFailed, match="delivery failed"):
            await transaction
        with pytest.raises(BlockingIOError):
            os.read(reader, 4096)
        assert backend.confirmed_level is None
    finally:
        if "transaction" in locals() and not transaction.done():
            transaction.cancel()
            with suppress(asyncio.CancelledError):
                await transaction
        os.close(reader)
        await writer.remove()
        await rig.close()


async def test_current_checked_transaction_delivers_to_real_fifo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A current checked transaction still reaches the FIFO after lock contention."""
    rig = make_rig(1)
    writer = AsyncNamedPipeWriter(str(tmp_path / "commands"))
    await writer.create()
    reader = os.open(writer.path, os.O_RDONLY | os.O_NONBLOCK)
    stream = rig.streams[0]
    stream.commands_pipe = writer
    queued = asyncio.Event()
    delivered = asyncio.Event()
    send = stream.send_cli_command
    write_atomic = writer.write_atomic

    async def send_after_queue(command: str, *, guard: Callable[[], bool] | None = None) -> bool:
        queued.set()
        return await send(command, guard=guard)

    async def record_delivery(data: bytes, *, guard: Callable[[], bool] | None = None) -> bool:
        result = await write_atomic(data, guard=guard)
        delivered.set()
        return result

    monkeypatch.setattr(stream, "send_cli_command", send_after_queue)
    monkeypatch.setattr(writer, "write_atomic", record_delivery)
    try:
        async with writer._write_lock:
            transaction = asyncio.create_task(stream.session_volume.apply(80))
            await asyncio.wait_for(queued.wait(), 1)
        await asyncio.wait_for(delivered.wait(), 1)
        assert b"ACTION=STREAM_VOLUME_APPLY" in os.read(reader, 4096)
        transaction.cancel()
        with pytest.raises(asyncio.CancelledError):
            await transaction
        assert stream.session_volume._pending is None
        assert stream.session_volume._pending_snapshot is None
    finally:
        if "transaction" in locals() and not transaction.done():
            transaction.cancel()
            with suppress(asyncio.CancelledError):
                await transaction
        os.close(reader)
        await writer.remove()
        await rig.close()


async def test_owner_change_drops_ordinary_checked_transaction_queued_on_real_fifo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retired owner cannot dispatch after the FIFO writer lock becomes available."""
    rig = make_rig(1)
    writer = AsyncNamedPipeWriter(str(tmp_path / "commands"))
    await writer.create()
    reader = os.open(writer.path, os.O_RDONLY | os.O_NONBLOCK)
    stream = rig.streams[0]
    stream.commands_pipe = writer
    queued = asyncio.Event()
    delivered = asyncio.Event()
    send = stream.send_cli_command
    write_atomic = writer.write_atomic

    async def send_after_queue(command: str, *, guard: Callable[[], bool] | None = None) -> bool:
        queued.set()
        return await send(command, guard=guard)

    async def record_delivery(data: bytes, *, guard: Callable[[], bool] | None = None) -> bool:
        result = await write_atomic(data, guard=guard)
        delivered.set()
        return result

    monkeypatch.setattr(stream, "send_cli_command", send_after_queue)
    monkeypatch.setattr(writer, "write_atomic", record_delivery)
    try:
        async with writer._write_lock:
            command = asyncio.create_task(
                rig.controller.cmd_volume_set(stream.player.player_id, 80)
            )
            await asyncio.wait_for(queued.wait(), 1)
            stream.session_volume.owner_changed()
        await asyncio.wait_for(delivered.wait(), 1)
        with pytest.raises(PlayerCommandFailed, match="delivery failed"):
            await command
        with pytest.raises(BlockingIOError):
            os.read(reader, 4096)
        queued.clear()
        delivered.clear()
        replacement = asyncio.create_task(
            rig.controller.cmd_volume_set(stream.player.player_id, 80)
        )
        await asyncio.wait_for(queued.wait(), 1)
        await asyncio.wait_for(delivered.wait(), 1)
        assert b"ACTION=STREAM_VOLUME_APPLY" in os.read(reader, 4096)
        replacement.cancel()
        with pytest.raises(asyncio.CancelledError):
            await replacement
        assert stream.session_volume._pending is None
        assert stream.session_volume._pending_snapshot is None
    finally:
        if "command" in locals() and not command.done():
            command.cancel()
            with suppress(asyncio.CancelledError):
                await command
        os.close(reader)
        await writer.remove()
        await rig.close()


async def test_real_child_stdout_fifo_round_trip(tmp_path: Path) -> None:
    """Exercise real process/stdout/FIFO plus real controller, without a receiver or network."""
    rig = make_rig(1)
    stream = rig.streams[0]
    pipe = tmp_path / "commands"
    stream.commands_pipe = AsyncNamedPipeWriter(str(pipe))
    cast("MagicMock", stream.prov.config).get_value.return_value = True
    capabilities_seen = asyncio.Event()
    script = (
        "import sys\n"
        "print('[STATUS] session_volume version=1', flush=True)\n"
        "print('[STATUS] capabilities live_metadata=1', flush=True)\n"
        "print('[STATUS] connected', file=sys.stderr, flush=True)\n"
        f"with open({str(pipe)!r}) as commands:\n"
        " fields = {}\n"
        " for line in commands:\n"
        "  key, value = line.strip().split('=', 1)\n"
        "  fields[key] = value\n"
        "  if key != 'ACTION': continue\n"
        "  if value == 'STREAM_VOLUME_APPLY':\n"
        "   print('[EVENT] stream_volume_result generation=' + fields['STREAM_VOLUME_GENERATION']"
        " + ' ordinal=' + fields['STREAM_VOLUME_ORDINAL'] + ' status=committed level='"
        " + str(min(61, int(fields['STREAM_VOLUME_LEVEL']))), flush=True)\n"
        "  if value == 'STOP': break\n"
        "  fields = {}\n"
    )
    try:
        with (
            patch.object(
                stream,
                "_build_cli_args",
                new=AsyncMock(return_value=[sys.executable, "-c", script]),
            ),
            patch.object(
                stream, "_parse_capabilities_status", side_effect=lambda _: capabilities_seen.set()
            ),
        ):
            await stream.connect()
            await asyncio.wait_for(capabilities_seen.wait(), 5)
            assert await stream.commands_pipe.wait_for_reader(2)
            assert stream.session_volume.enabled
            assert stream.session_volume.confirmed_level is None
            await stream.session_volume.apply(60)
            await rig.bind()
            await rig.request(630)
            assert stream.player.volume_level == 61
    finally:
        await stream.stop(force=True)
        await rig.close()
    assert not pipe.exists()
