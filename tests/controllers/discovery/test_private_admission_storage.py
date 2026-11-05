"""Offline persistence checks for private discovery admission."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from music_assistant.controllers.discovery import controller as discovery_module
from music_assistant.helpers.private_trial import CONF_PRIVATE_TRIAL_TARGET
from music_assistant.mass import MusicAssistant
from music_assistant.providers.airplay import provider as provider_module
from music_assistant.providers.airplay.provider import AirPlayProvider

TARGET = "aabbccddeeff@test"
PLAYER_ID = "apaabbccddeeff"


@pytest.fixture
async def private_discovery(
    mass_minimal: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> MusicAssistant:
    """Start the real discovery controller with external services isolated."""
    mass = mass_minimal
    monkeypatch.setattr(discovery_module, "is_trial_target", lambda: True)
    monkeypatch.setattr(provider_module, "is_trial_target", lambda: True)
    monkeypatch.setattr(mass.discovery, "_setup_mdns_browser", AsyncMock())
    monkeypatch.setattr(mass.discovery, "_register_mass_service", AsyncMock())
    monkeypatch.setattr(mass.discovery, "_schedule_periodic_upnp_discovery", lambda: None)
    await mass.discovery.setup(await mass.config.get_core_config("discovery"))
    return mass


def persisted_values(mass: MusicAssistant) -> dict[str, object]:
    """Read persisted discovery values written by the real config controller."""
    data = json.loads(Path(mass.config.filename).read_text())
    values = data["core"]["discovery"]["values"]
    assert isinstance(values, dict)
    return values


async def test_setup_flushes_reset_before_browser(
    mass_minimal: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The browser cannot start before the explicit raw reset is durable."""
    mass = mass_minimal
    monkeypatch.setattr(discovery_module, "is_trial_target", lambda: True)
    mass.config.set_raw_core_config_value("discovery", CONF_PRIVATE_TRIAL_TARGET, TARGET)
    await mass.config.async_save()

    async def browser() -> None:
        """Inspect the settings at the browser boundary."""
        assert persisted_values(mass)[CONF_PRIVATE_TRIAL_TARGET] == ""

    mock_browser = AsyncMock(side_effect=browser)
    monkeypatch.setattr(mass.discovery, "_setup_mdns_browser", mock_browser)
    monkeypatch.setattr(mass.discovery, "_register_mass_service", AsyncMock())
    monkeypatch.setattr(mass.discovery, "_schedule_periodic_upnp_discovery", lambda: None)
    await mass.discovery.setup(await mass.config.get_core_config("discovery"))
    mock_browser.assert_awaited_once()
    assert mass.config.get_raw_core_config_value("discovery", CONF_PRIVATE_TRIAL_TARGET) == ""


@pytest.mark.parametrize(("players", "state"), [(None, "unsafe"), ({}, "absent")])
async def test_preflight_real_player_store(
    private_discovery: MusicAssistant, players: dict[str, object] | None, state: str
) -> None:
    """Null and empty player stores have distinct safety outcomes."""
    mass = private_discovery
    mass.config.set("players", players)
    provider = AirPlayProvider.__new__(AirPlayProvider)
    provider.mass = mass
    provider.__dict__["get_players"] = list
    result = await provider.get_private_trial_admission_preflight(PLAYER_ID)
    assert result["config_state"] == state
    assert result["target_unset"] is True
    mass.config.remove("players")
    assert (await provider.get_private_trial_admission_preflight(PLAYER_ID))[
        "config_state"
    ] == "unsafe"


async def test_core_save_clear_and_deferred_flush(
    private_discovery: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core saves update live state before a deliberately blocked disk flush."""
    mass = private_discovery
    entered = asyncio.Event()
    release = asyncio.Event()
    real_save = mass.config.async_save

    async def parked_save() -> None:
        """Hold the scheduled save until the test releases it."""
        entered.set()
        await release.wait()
        await real_save()

    monkeypatch.setattr(mass.config, "async_save", parked_save)
    try:
        result = await mass.config.save_core_config(
            "discovery", {CONF_PRIVATE_TRIAL_TARGET: TARGET}
        )
        await asyncio.wait_for(entered.wait(), 5)
        assert not release.is_set()
        assert result.get_value(CONF_PRIVATE_TRIAL_TARGET) == TARGET
        assert (
            mass.config.get_raw_core_config_value("discovery", CONF_PRIVATE_TRIAL_TARGET) == TARGET
        )
        assert mass.discovery.trial_target() is not None
    finally:
        release.set()
    await real_save()
    assert persisted_values(mass)[CONF_PRIVATE_TRIAL_TARGET] == TARGET

    result = await mass.config.save_core_config("discovery", {CONF_PRIVATE_TRIAL_TARGET: ""})
    assert result.get_value(CONF_PRIVATE_TRIAL_TARGET) == ""
    assert mass.discovery.trial_target() is None
    assert CONF_PRIVATE_TRIAL_TARGET not in mass.config.get("core/discovery/values")
    provider = AirPlayProvider.__new__(AirPlayProvider)
    provider.mass = mass
    provider.__dict__["get_players"] = list
    assert (await provider.get_private_trial_admission_preflight(PLAYER_ID))[
        "target_unset"
    ] is False
    await mass.config.save_core_config("discovery", {CONF_PRIVATE_TRIAL_TARGET: ""})
    await real_save()
    assert CONF_PRIVATE_TRIAL_TARGET not in persisted_values(mass)
