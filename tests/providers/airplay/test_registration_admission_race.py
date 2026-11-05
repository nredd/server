"""
Regression tests for admission changes during player registration.

References:
- https://github.com/nredd/server/issues/66
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.controllers.players import PlayerController
from music_assistant.helpers.private_trial import parse_trial_target
from music_assistant.mass import MusicAssistant
from music_assistant.providers.airplay import provider as provider_module
from music_assistant.providers.airplay.provider import AirPlayProvider
from music_assistant.providers.airplay.sendspin_bridge import SendspinBridgeManager

PLAYER_ID = "ap0123456789ab"
TARGET = "0123456789ab@Test Receiver"


@pytest.fixture
def setup_context(
    mass_minimal: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> tuple[AirPlayProvider, MagicMock, list[str], MagicMock]:
    """Create a provider using the real config controller and real player constructor."""
    provider = AirPlayProvider.__new__(AirPlayProvider)
    provider.mass = mass_minimal
    provider.logger = logging.getLogger(__name__)
    provider.unloading = False
    provider.config = MagicMock(instance_id="airplay--test")
    provider.manifest = MagicMock(domain="airplay")
    target = [TARGET]
    monkeypatch.setattr(provider_module, "is_trial_target", lambda: True)
    monkeypatch.setattr(
        mass_minimal.discovery, "trial_target", lambda: parse_trial_target(target[0])
    )
    monkeypatch.setattr(provider, "_is_own_airplay_receiver", AsyncMock(return_value=False))
    mass_minimal.players = MagicMock()
    mass_minimal.players.get_player.return_value = None
    mass_minimal.streams = MagicMock(publish_ip="192.0.2.2")
    info = MagicMock(type="_raop._tcp.local.", port=0)
    info.parsed_addresses.return_value = ["192.0.2.1"]
    info.decoded_properties = {}
    monkeypatch.setattr(provider_module, "get_model_info", lambda _: ("Other", "Receiver"))
    monkeypatch.setattr(
        provider_module,
        "get_primary_ip_address_from_zeroconf",
        lambda *_args, **_kwargs: "192.0.2.1",
    )
    create = MagicMock(wraps=mass_minimal.config.create_default_player_config)
    monkeypatch.setattr(mass_minimal.config, "create_default_player_config", create)
    provider._bridge_manager = MagicMock(evaluate_bridge=AsyncMock())
    provider.dashboards = MagicMock()
    return provider, info, target, create


@pytest.mark.parametrize("change", ["clear", "unload"])
async def test_change_during_discovery_prevents_config_creation(
    setup_context: tuple[AirPlayProvider, MagicMock, list[str], MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    """A change while mDNS waits must precede the actual player constructor."""
    provider, info, target, create = setup_context
    started, proceed = asyncio.Event(), asyncio.Event()

    async def find(*_args: object, **_kwargs: object) -> None:
        started.set()
        await proceed.wait()

    monkeypatch.setattr(provider.mass.discovery, "async_find_mdns_service", find)
    task = asyncio.create_task(provider._setup_player(PLAYER_ID, "Test Receiver", info))
    await asyncio.wait_for(started.wait(), 2)
    if change == "clear":
        target[0] = ""
    else:
        provider.unloading = True
    proceed.set()
    await task
    create.assert_not_called()
    assert isinstance(provider.mass.players, MagicMock)
    provider.mass.players.register.assert_not_called()
    assert provider.mass.config.get(f"players/{PLAYER_ID}") is None


@pytest.mark.parametrize("private", [True, False])
async def test_admitted_player_is_constructed_and_registered(
    setup_context: tuple[AirPlayProvider, MagicMock, list[str], MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    private: bool,
) -> None:
    """Valid private and public players still create config and reach registration."""
    provider, info, _target, create = setup_context
    monkeypatch.setattr(provider_module, "is_trial_target", lambda: private)
    monkeypatch.setattr(
        provider.mass.discovery, "async_find_mdns_service", AsyncMock(return_value=None)
    )
    register = AsyncMock()
    monkeypatch.setattr(provider.mass.players, "register", register)
    await provider._setup_player(PLAYER_ID, "Test Receiver", info)
    create.assert_called_once()
    assert provider.mass.config.get(f"players/{PLAYER_ID}") is not None
    register.assert_awaited_once()
    assert isinstance(provider._bridge_manager, MagicMock)
    provider._bridge_manager.evaluate_bridge.assert_awaited_once()


@pytest.mark.parametrize("change", ["clear", "unload", "none"])
async def test_bridge_lock_rechecks_admission(
    setup_context: tuple[AirPlayProvider, MagicMock, list[str], MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    """Bridge setup rechecks admission after waiting for the manager lock."""
    provider, _info, target, _create = setup_context
    mass = provider.mass
    monkeypatch.setattr(mass, "subscribe", MagicMock())
    monkeypatch.setattr(mass, "get_provider", MagicMock(return_value=MagicMock()))
    manager = SendspinBridgeManager(provider)
    player = MagicMock(player_id=PLAYER_ID)
    assert isinstance(mass.players, MagicMock)
    mass.players.get_player.return_value = player
    monkeypatch.setattr(manager, "_should_have_bridge", MagicMock(return_value=True))
    bridge = MagicMock(is_registered=True, start=AsyncMock())
    create = MagicMock(return_value=bridge)
    monkeypatch.setattr(manager, "_create_bridge", create)
    await manager._lock.acquire()
    task = asyncio.create_task(manager.setup_bridge(player))
    await asyncio.sleep(0)
    if change == "clear":
        target[0] = ""
    elif change == "unload":
        provider.unloading = True
    manager._lock.release()
    await task
    assert create.call_count == (1 if change == "none" else 0)


@pytest.mark.parametrize("change", ["clear", "unload"])
async def test_change_during_bridge_evaluation_cleans_up(
    setup_context: tuple[AirPlayProvider, MagicMock, list[str], MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    """Admission changes during bridge evaluation remove only the constructed player."""
    provider, info, target, _create = setup_context
    monkeypatch.setattr(
        provider.mass.discovery, "async_find_mdns_service", AsyncMock(return_value=None)
    )
    players = provider.mass.players
    assert isinstance(players, MagicMock)
    players.register = AsyncMock()
    players.unregister = AsyncMock()
    started, proceed = asyncio.Event(), asyncio.Event()

    async def evaluate(_player: object) -> None:
        started.set()
        await proceed.wait()

    monkeypatch.setattr(
        provider._bridge_manager, "evaluate_bridge", AsyncMock(side_effect=evaluate)
    )
    task = asyncio.create_task(provider._setup_player(PLAYER_ID, "Test Receiver", info))
    await asyncio.wait_for(started.wait(), 2)
    if change == "clear":
        target[0] = ""
    else:
        provider.unloading = True
    proceed.set()
    await task
    assert players.register.await_args is not None
    player = players.register.await_args.args[0]
    players.unregister.assert_awaited_once_with(PLAYER_ID, expected_player=player)
    assert isinstance(provider.dashboards, MagicMock)
    provider.dashboards.setup_player.assert_not_called()


@pytest.mark.parametrize("change", ["clear", "unload"])
async def test_change_during_registration_removes_exact_player(
    setup_context: tuple[AirPlayProvider, MagicMock, list[str], MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    """A change inside real registration removes the stale player and skips bridge work."""
    provider, info, target, create = setup_context
    mass = provider.mass
    monkeypatch.setattr(mass.discovery, "async_find_mdns_service", AsyncMock(return_value=None))
    controller = PlayerController(mass)
    mass.players = controller
    monkeypatch.setattr(mass.cache, "get", AsyncMock(return_value=False))
    mass.player_queues = MagicMock(on_player_register=AsyncMock())
    mass.player_queues.get.return_value = None
    mass.metadata = MagicMock()
    monkeypatch.setattr(controller, "_resolve_mac_addresses", AsyncMock())
    monkeypatch.setattr(controller, "_save_underlying_player_id", MagicMock())
    monkeypatch.setattr(controller, "_evaluate_protocol_links", MagicMock())
    monkeypatch.setattr(controller, "_schedule_update_all_players", MagicMock())
    monkeypatch.setattr(mass, "signal_event", MagicMock())
    started, proceed = asyncio.Event(), asyncio.Event()

    async def get_config(player_id: str) -> object:
        started.set()
        await proceed.wait()
        return mass.config.get_base_player_config(player_id, provider.instance_id)

    monkeypatch.setattr(mass.config, "get_player_config", get_config)
    task = asyncio.create_task(provider._setup_player(PLAYER_ID, "Test Receiver", info))
    await asyncio.wait_for(started.wait(), 2)
    create.assert_called_once()
    assert controller.get_player(PLAYER_ID) is not None
    if change == "clear":
        target[0] = ""
    else:
        provider.unloading = True
    proceed.set()
    await task
    assert controller.get_player(PLAYER_ID) is None
    assert isinstance(provider._bridge_manager, MagicMock)
    assert isinstance(provider.dashboards, MagicMock)
    provider._bridge_manager.evaluate_bridge.assert_not_awaited()
    provider.dashboards.setup_player.assert_not_called()
