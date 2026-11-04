"""Tests for the private read-only discovery admission preflight."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from music_assistant.providers.airplay import provider as provider_module
from music_assistant.providers.airplay.provider import AirPlayProvider

PLAYER_ID = "ap0123456789ab"


@pytest.fixture(autouse=True)
def _trial_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the preflight tests in the private profile."""
    monkeypatch.setattr(provider_module, "is_trial_target", lambda: True)


def _provider(raw: object) -> tuple[AirPlayProvider, MagicMock, dict[str, Any]]:
    """Create a provider with isolated raw config."""
    provider = AirPlayProvider.__new__(AirPlayProvider)
    mass = MagicMock()
    provider.mass = mass
    store = {
        "players": raw,
        "core": {"discovery": {"values": {"private_trial_target": ""}}},
    }
    mass.config.get.side_effect = store.get
    mass.discovery.config.values = {"private_trial_target": SimpleNamespace(value="")}
    setattr(provider, "get_players", MagicMock(return_value=[]))
    return provider, mass, store


@pytest.mark.parametrize(
    "raw", [{}, {PLAYER_ID: {}}, {PLAYER_ID: {"setup_data": {}, "values": {}}}]
)
async def test_safe_snapshot_is_read_only(raw: object) -> None:
    """Absent and empty setup snapshots are safe without mutating or contacting anything."""
    provider, mass, store = _provider(raw)
    before = deepcopy(store)
    result = await provider.get_private_trial_admission_preflight(PLAYER_ID)
    assert result == {
        "version": 1,
        "config_state": "absent" if not raw else "present",
        "setup_data_empty": True,
        "credentials_absent": True,
        "default_volume_bounds": True,
        "target_unset": True,
        "no_current_streams": True,
    }
    assert store == before
    mass.config.set.assert_not_called()
    mass.config.get_player_config.assert_not_called()


@pytest.mark.parametrize(
    "raw",
    [None, [], {PLAYER_ID: None}, {PLAYER_ID: {"values": []}}, {PLAYER_ID: {"setup_data": None}}],
)
async def test_malformed_snapshot_fails_closed(raw: object) -> None:
    """Malformed data is never reported as absent."""
    provider, _, _ = _provider(raw)
    assert (await provider.get_private_trial_admission_preflight(PLAYER_ID))[
        "config_state"
    ] == "unsafe"


@pytest.mark.parametrize("setup", [{"unknown": None}, {"native_mrp_credentials": "secret"}])
async def test_any_setup_data_is_unsafe(setup: dict[str, Any]) -> None:
    """Unknown and known setup data cannot pass the gate."""
    provider, _, _ = _provider({PLAYER_ID: {"setup_data": setup}})
    result = await provider.get_private_trial_admission_preflight(PLAYER_ID)
    assert result["setup_data_empty"] is False
    assert result["credentials_absent"] is False
    assert "secret" not in str(result)


@pytest.mark.parametrize(
    "key",
    [
        "companion_credentials",
        "mrp_credentials",
        "native_mrp_credentials",
        "raop_credentials",
        "airplay_credentials",
        "password",
    ],
)
async def test_legacy_credentials_fail_closed(key: str) -> None:
    """Legacy credentials in values must not escape migration into discovery."""
    provider, _, _ = _provider({PLAYER_ID: {"values": {key: "secret"}}})
    assert (await provider.get_private_trial_admission_preflight(PLAYER_ID))[
        "credentials_absent"
    ] is False


@pytest.mark.parametrize(
    "values",
    [
        {"min_volume": False},
        {"max_volume": "100"},
        {"min_volume": 1},
        {"max_volume": 99},
        {"min_volume": None},
    ],
)
async def test_nondefault_bounds_fail_closed(values: dict[str, Any]) -> None:
    """Only exact integer defaults are safe."""
    provider, _, _ = _provider({PLAYER_ID: {"values": values}})
    assert (await provider.get_private_trial_admission_preflight(PLAYER_ID))[
        "default_volume_bounds"
    ] is False


async def test_target_and_streams_fail_closed() -> None:
    """A configured target or any current stream prevents admission."""
    provider, mass, store = _provider({})
    store["core"]["discovery"]["values"]["private_trial_target"] = "set"
    setattr(provider, "get_players", MagicMock(return_value=[SimpleNamespace(stream=object())]))
    result = await provider.get_private_trial_admission_preflight(PLAYER_ID)
    assert result["target_unset"] is False
    assert result["no_current_streams"] is False
    mass.config.set.assert_not_called()


async def test_direct_handler_rejects_other_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    """Direct invocation outside the private profile is unavailable."""
    provider, mass, _ = _provider({})
    monkeypatch.setattr(provider_module, "is_trial_target", lambda: False)
    with pytest.raises(RuntimeError, match="unavailable"):
        await provider.get_private_trial_admission_preflight(PLAYER_ID)
    mass.config.get.assert_not_called()


@pytest.mark.parametrize(
    "player_id", ["apABCDEF123456", "ap123", "ap0123456789ab/values", "0123456789ab"]
)
async def test_rejects_noncanonical_player_id(player_id: str) -> None:
    """Only a canonical lowercase AirPlay ID can be used as a key."""
    provider, mass, _ = _provider({})
    with pytest.raises(ValueError, match="Invalid private trial player ID"):
        await provider.get_private_trial_admission_preflight(player_id)
    mass.config.get.assert_not_called()
