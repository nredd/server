"""
Offline bridge ownership and accepted-volume regressions using the installed SDK.

References:
    - airplay-cli/build/session-volume-contract.md (frozen IPC v1)
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiosendspin.server import SendspinServer
from music_assistant_models.enums import PlayerFeature, PlayerType
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.constants import CONF_MAX_VOLUME, CONF_MIN_VOLUME, CONF_VOLUME_CONTROL
from music_assistant.controllers.players.constants import PlayerLockPurpose
from music_assistant.providers.airplay.provider import AirPlayProvider
from music_assistant.providers.airplay.sendspin_bridge import (
    SendspinAirPlayBridge,
    SendspinBridgeManager,
)
from music_assistant.providers.sendspin.bridge_role import BridgePlayerRole
from music_assistant.providers.sendspin.player import SendspinPlayer
from music_assistant.providers.sendspin.provider import SendspinProvider
from tests.common import MockPlayer, MockProvider
from tests.providers.airplay.test_session_volume import VolumeRig, make_rig


@dataclass
class BridgeRig:
    """Native fixture API retained, with real bridge/SDK/client/MA player seams."""

    rig: VolumeRig
    bridges: list[SendspinAirPlayBridge]
    players: list[SendspinPlayer]
    roles: list[BridgePlayerRole]


async def make_bridge_rig(count: int = 2) -> BridgeRig:
    """Register real in-process SDK clients without starting a server or device transport."""
    rig = make_rig(count)
    provider = cast("AirPlayProvider", rig.streams[0].prov)
    cast("MagicMock", provider.mass.create_task).side_effect = lambda coro, **_kwargs: (
        asyncio.create_task(coro)
    )
    server = SendspinServer(
        asyncio.get_running_loop(), MagicMock(), "offline", MagicMock(), pairing_store=MagicMock()
    )
    manager = SendspinBridgeManager(provider)
    cast("MagicMock", provider).bridge_manager = manager
    sendspin = MagicMock()
    sendspin.mass = provider.mass
    sendspin.domain = "sendspin"
    sendspin.instance_id = "sendspin-test"
    sendspin.server_api = server
    cast("MagicMock", provider.mass.get_provider).return_value = sendspin
    bridges: list[SendspinAirPlayBridge] = []
    roles: list[BridgePlayerRole] = []
    players: list[SendspinPlayer] = []
    for stream in rig.streams:
        stream.session = None
        stream.player._attr_group_members = []
        stream.player.update_state()
        bridge = SendspinAirPlayBridge(provider, stream.player, server)
        await bridge.start()
        bridge._airplay_stream = stream
        manager._bridges[stream.player.player_id] = bridge
        assert bridge.bridge_client_id is not None
        server._external_stream_start_cbs[bridge.bridge_client_id] = lambda _request: None
        player = SendspinPlayer(cast("SendspinProvider", sendspin), bridge.bridge_client_id)
        cast("MagicMock", player).send_current_media_metadata = AsyncMock()
        rig.controller._players[player.player_id] = player
        player._attr_powered = True
        player.set_initialized()
        player.update_state(force_update=True, signal_event=False)
        assert bridge._bridge_role is not None
        bridges.append(bridge)
        bridge._bridge_role._on_stream_start_cb = None
        bridge._bridge_role._on_stream_end_cb = None
        roles.append(bridge._bridge_role)
        players.append(player)
    group = players[0].api.group
    for player in players[1:]:
        await group.add_client(player.api)
    group.start_stream()
    # Explicit state refreshes substitute for the server's ordinary debounced update loop.
    for player in players:
        player.update_state(force_update=True, signal_event=False)
    for index, bridge in enumerate(bridges):
        await bridge.apply_volume_confirmed(60 if index == 0 else 30)
    rig.commands.clear()
    rig.reached.clear()
    return BridgeRig(rig, bridges, players, roles)


@pytest.mark.parametrize("count", [1, 2])
async def test_bridge_solo_follower_and_unequal_group(count: int) -> None:
    """The exact bridge owner reuses Core interpolation and actual accepted receipts."""
    fixture = await make_bridge_rig(count)
    rig = fixture.rig
    try:
        backend = await rig.bind(count - 1)
        assert backend.binding is not None
        assert backend.binding.snapshot.owner_id == fixture.players[0].player_id
        rig.clamp = 61
        await rig.request(630, index=count - 1)
        assert [stream.session_volume.confirmed_level for stream in rig.streams] == [61, 35][
            :count
        ], rig.commands
        completion = next(cmd for cmd in rig.commands if "SESSION_VOLUME_COMPLETE" in cmd)
        assert "SESSION_VOLUME_RESULT=committed" in completion
        assert "SESSION_VOLUME_MILLI=610" in completion
    finally:
        await rig.close()


async def test_regular_bridge_command_awaits_receipt_without_double_scale_or_feedback_command() -> (
    None
):
    """A parent-scaled raw target is not rescaled by AirPlay or SDK feedback."""
    fixture = await make_bridge_rig(1)
    rig = fixture.rig
    try:
        player = fixture.players[0]
        cast("MagicMock", player.mass.config.get_raw_player_config_value).side_effect = (
            lambda _id, key, default=None: {CONF_MIN_VOLUME: 20, CONF_MAX_VOLUME: 80}.get(
                key, default
            )
        )
        backend = rig.streams[0].session_volume
        await fixture.bridges[0].apply_volume_confirmed(60)
        rig.commands.clear()
        rig.reached.clear()
        await rig.bind()
        rig.delay = asyncio.Event()
        task = asyncio.create_task(rig.controller.cmd_volume_set(player.player_id, 25))
        await rig.reached.wait()
        assert not task.done()
        assert fixture.roles[0].get_player_volume() == 60
        rig.delay.set()
        await task
        assert backend.confirmed_level == 35
        assert fixture.roles[0].get_player_volume() == 35
        assert sum("ACTION=STREAM_VOLUME_APPLY" in cmd for cmd in rig.commands) == 1
        with patch.object(rig.controller, "cmd_volume_set") as command:
            fixture.bridges[0].sync_role_volume_state()
            fixture.roles[0].update_player_state(volume=49)
            command.assert_not_called()
        assert player.volume_level == 49
        assert backend.confirmed_level == 35
    finally:
        await rig.close()


@pytest.mark.parametrize(
    "change", ["missing", "mixed", "replaced", "membership", "native", "cycle"]
)
async def test_bridge_unavailable_preflight_has_zero_actions(change: str) -> None:
    """Unresolved SDK ownership or any unconfirmed member rejects the entire group."""
    fixture = await make_bridge_rig()
    rig = fixture.rig
    try:
        if change == "missing":
            fixture.bridges[0]._sendspin_client = None
        elif change == "mixed":
            rig.streams[1].session_volume.supported = False
        elif change == "replaced":
            fixture.bridges[0]._airplay_stream = rig.streams[1]
        elif change == "native":
            rig.streams[0].session = MagicMock(
                sync_clients=[stream.player for stream in rig.streams]
            )
        elif change == "cycle":
            fixture.players[0].state.synced_to = fixture.players[0].player_id
        else:
            fixture.players[0].api.group.clients.pop()
        with pytest.raises(PlayerCommandFailed):
            await rig.bind()
        assert rig.commands == []
    finally:
        await rig.close()


@pytest.mark.parametrize("purpose", [PlayerLockPurpose.GROUP_VOLUME, PlayerLockPurpose.VOLUME])
@pytest.mark.parametrize("replace_bridge", [False, True])
async def test_bridge_takeover_waiting_for_core_locks(
    purpose: PlayerLockPurpose, replace_bridge: bool
) -> None:
    """A source replaced while blocked cannot send to its successor."""
    fixture = await make_bridge_rig()
    rig = fixture.rig
    try:
        await rig.bind()
        rig.commands.clear()
        async with rig.controller.get_player_lock(fixture.players[0].player_id, purpose):
            task = asyncio.create_task(rig.request(400))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            if replace_bridge:
                fixture.bridges[0]._sendspin_client = fixture.players[1].api
            else:
                fixture.players[0].api.group.start_stream()
        await task
        assert not any("ACTION=STREAM_VOLUME_APPLY" in cmd for cmd in rig.commands)
    finally:
        await rig.close()


async def test_bridge_failure_fans_unknown_to_all_bindings() -> None:
    """Failed regular MA setters notify leader and follower without telemetry commands."""
    fixture = await make_bridge_rig()
    rig = fixture.rig
    try:
        backends = [await rig.bind(index) for index in range(2)]
        rig.commands.clear()
        rig.fail_member = 0
        with pytest.raises(PlayerCommandFailed):
            await rig.controller.cmd_volume_set(fixture.players[0].player_id, 80)
        for backend in backends:
            assert backend._feedback_task is not None
            await backend._feedback_task
            assert any(
                f"SESSION_VOLUME_GENERATION={backend.generation}\n" in cmd
                and "SESSION_VOLUME_KNOWN=0" in cmd
                for cmd in rig.commands
            )
        assert sum("ACTION=STREAM_VOLUME_APPLY" in cmd for cmd in rig.commands) == 1
    finally:
        await rig.close()


async def test_bridge_active_group_preserves_unequal_balance() -> None:
    """An explicit MA group, not the transport target, owns both checked SDK actuators."""
    fixture = await make_bridge_rig()
    rig = fixture.rig
    try:
        owner = MockPlayer(
            MockProvider("group", mass=cast("MagicMock", rig.controller.mass)),
            "active-group",
            "Group",
            PlayerType.GROUP,
        )
        owner._attr_available = True
        owner._attr_powered = True
        owner._attr_group_members = [player.player_id for player in fixture.players]
        rig.controller._players[owner.player_id] = owner
        owner.set_initialized()
        owner.update_state(force_update=True, signal_event=False)
        for player in fixture.players:
            player.update_state(force_update=True, signal_event=False)
        for index, bridge in enumerate(fixture.bridges):
            await bridge.apply_volume_confirmed(60 if index == 0 else 30)
        backend = await rig.bind(1)
        assert backend.binding is not None
        assert backend.binding.snapshot.owner_id == owner.player_id
        await rig.request(200, index=1)
        assert [stream.session_volume.confirmed_level for stream in rig.streams] == [20, 10]
    finally:
        await rig.close()


async def test_bridge_visible_native_airplay_actuator_is_confirmed() -> None:
    """A native AirPlay volume control is eligible only in the exact owning SDK source."""
    fixture = await make_bridge_rig(1)
    rig = fixture.rig
    try:
        parent = rig.streams[0].player
        output = fixture.players[0]
        output.set_protocol_parent_id(parent.player_id)
        output.update_state(force_update=True, signal_event=False)
        await fixture.bridges[0].apply_volume_confirmed(60)
        backend = await rig.bind()
        assert backend.binding is not None
        assert backend.binding.snapshot.owner_id == parent.player_id
        await rig.request(400)
        assert backend.confirmed_level == 40
        await rig.controller.cmd_volume_set(parent.player_id, 30)
        assert backend.confirmed_level == 30
    finally:
        await rig.close()


@pytest.mark.parametrize("grouped", [False, True])
async def test_bridge_visible_parent_and_active_group(grouped: bool) -> None:
    """Resolve explicit visible ownership, scaling the parent once, never the transport."""
    fixture = await make_bridge_rig(1)
    rig = fixture.rig
    try:
        mass = cast("MagicMock", rig.controller.mass)
        output = fixture.players[0]
        parent = MockPlayer(MockProvider("native", mass=mass), "visible", "Visible")
        parent._attr_supported_features = {PlayerFeature.VOLUME_SET}
        parent._attr_available = True
        parent._attr_powered = True
        rig.controller._players[parent.player_id] = parent
        mass.config.get_raw_player_config_value.side_effect = lambda player_id, key, default=None: (
            {CONF_VOLUME_CONTROL: output.player_id, CONF_MIN_VOLUME: 20, CONF_MAX_VOLUME: 80}.get(
                key, default
            )
            if player_id == parent.player_id
            else default
        )
        parent.set_initialized()
        output.set_protocol_parent_id(parent.player_id)
        parent.update_state(force_update=True, signal_event=False)
        output.update_state(force_update=True, signal_event=False)
        owner = parent
        if grouped:
            owner = MockPlayer(MockProvider("group", mass=mass), "group", "Group", PlayerType.GROUP)
            owner._attr_available = True
            owner._attr_powered = True
            owner._attr_group_members = [parent.player_id]
            rig.controller._players[owner.player_id] = owner
            owner.set_initialized()
            owner.update_state(force_update=True, signal_event=False)
            parent.update_state(force_update=True, signal_event=False)
        await fixture.bridges[0].apply_volume_confirmed(50)
        backend = await rig.bind()
        assert backend.binding is not None
        assert backend.binding.snapshot.owner_id == owner.player_id
        rig.commands.clear()
        await rig.request(250)
        assert backend.confirmed_level == 35
        assert any("SESSION_VOLUME_MILLI=250" in cmd for cmd in rig.commands)
        await rig.controller.cmd_volume_set(parent.player_id, 75)
        assert backend.confirmed_level == 65
    finally:
        await rig.close()


@pytest.mark.parametrize("cancel", [False, True])
async def test_bridge_takeover_or_cancel_after_send_never_commits(cancel: bool) -> None:
    """A late accepted actuator result cannot certify a replaced or cancelled source."""
    fixture = await make_bridge_rig()
    rig = fixture.rig
    try:
        await rig.bind()
        await rig.bind(1)
        rig.commands.clear()
        rig.delay = asyncio.Event()
        task = asyncio.create_task(rig.request(700))
        await rig.reached.wait()
        if cancel:
            task.cancel()
        else:
            fixture.players[0].api.group.start_stream()
        rig.delay.set()
        with suppress(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        assert all(stream.session_volume.confirmed_level is None for stream in rig.streams)
        assert not any("SESSION_VOLUME_RESULT=committed" in cmd for cmd in rig.commands)
        assert any(
            "SESSION_VOLUME_KNOWN=0" in cmd or "SESSION_VOLUME_UNBIND" in cmd
            for cmd in rig.commands
        )
    finally:
        await rig.close()


async def test_bridge_startup_unknown_zero_and_checked_mute() -> None:
    """Unknown never means zero, and regular checked mute feedback cannot redispatch."""
    fixture = await make_bridge_rig(1)
    rig = fixture.rig
    try:
        backend = rig.streams[0].session_volume
        backend._discard_receipt()
        await rig.bind()
        assert "SESSION_VOLUME_KNOWN=0" in rig.commands[-1]
        await rig.request(300)
        assert "SESSION_VOLUME_RESULT=rejected" in rig.commands[-1]
        await fixture.bridges[0].apply_volume_confirmed(0)
        await backend.publish_state()
        assert "SESSION_VOLUME_MILLI=0" in rig.commands[-1]
        rig.commands.clear()
        await rig.controller.cmd_volume_mute(fixture.players[0].player_id, True)
        assert fixture.roles[0].get_player_muted() is True
        await rig.controller.cmd_volume_set(fixture.players[0].player_id, 70)
        assert backend.confirmed_level == 0
        assert fixture.roles[0].get_player_volume() == 0
        await rig.controller.cmd_volume_mute(fixture.players[0].player_id, False)
        assert fixture.roles[0].get_player_muted() is False
        assert not any(cmd.startswith("VOLUME=") for cmd in rig.commands)
        assert sum("ACTION=STREAM_VOLUME_APPLY" in cmd for cmd in rig.commands) == 3
    finally:
        await rig.close()
