"""Tests for the private native-volume capture API."""

from __future__ import annotations

from collections import deque
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.auth import Scope

from music_assistant.providers.airplay import provider as provider_module
from music_assistant.providers.airplay import trial_binary
from music_assistant.providers.airplay.private_volume_capture import PrivateVolumeCapture
from music_assistant.providers.airplay.provider import AirPlayProvider

_COMMAND = "airplay/private_native_volume_capture/get"
_READINESS_COMMAND = "airplay/private_native_volume_capture/readiness/get"


def _provider() -> tuple[AirPlayProvider, MagicMock]:
    """Build a bare provider with only private capture state."""
    provider = AirPlayProvider.__new__(AirPlayProvider)
    mass = MagicMock()
    provider.mass = mass
    provider._private_capture_history = deque(maxlen=16)
    provider._unregister_private_capture_api = None
    provider._unregister_private_capture_readiness_api = None
    return provider, mass


async def test_registers_fixed_scoped_route_only_for_trial_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The private route has one fixed name and system-management scope."""
    provider, mass = _provider()
    unregister_capture = MagicMock()
    unregister_readiness = MagicMock()
    mass.register_api_command.side_effect = (unregister_capture, unregister_readiness)
    monkeypatch.setattr(provider_module, "is_trial_target", lambda: True)

    await provider.loaded_in_mass()

    assert mass.register_api_command.call_args_list == [
        (
            (_COMMAND, provider.get_private_native_volume_capture),
            {"required_scope": Scope.SYSTEM_MANAGE},
        ),
        (
            (_READINESS_COMMAND, provider.get_private_native_volume_capture_readiness),
            {"required_scope": Scope.SYSTEM_MANAGE},
        ),
    ]
    assert provider._unregister_private_capture_api is unregister_capture
    assert provider._unregister_private_capture_readiness_api is unregister_readiness


async def test_fixed_route_collision_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """An existing fixed name aborts loading instead of replacing or suffixing a route."""
    provider, mass = _provider()
    mass.register_api_command.side_effect = RuntimeError("already registered")
    monkeypatch.setattr(provider_module, "is_trial_target", lambda: True)

    with pytest.raises(RuntimeError, match="already registered"):
        await provider.loaded_in_mass()

    assert provider._unregister_private_capture_api is None


async def test_does_not_register_route_outside_trial_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normal profiles never expose the private capture route."""
    provider, mass = _provider()
    monkeypatch.setattr(provider_module, "is_trial_target", lambda: False)

    await provider.loaded_in_mass()

    mass.register_api_command.assert_not_called()


async def test_direct_handlers_reject_nontrial_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """Direct calls cannot bypass either private-target registration gate."""
    provider, _ = _provider()
    monkeypatch.setattr(provider_module, "is_trial_target", lambda: False)

    with pytest.raises(RuntimeError, match="unavailable outside the trial target"):
        await provider.get_private_native_volume_capture()
    with pytest.raises(RuntimeError, match="unavailable outside the trial target"):
        await provider.get_private_native_volume_capture_readiness()


async def test_readiness_handler_returns_only_required_live_receipts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Readiness excludes diagnostics, pairing state, identities, paths, and arguments."""
    provider, _ = _provider()
    provider._ptp_daemon = MagicMock()
    provider._trial_identity = {"check_outcome": "passed"}
    provenance = {"status": "verified-installer-provenance", "commit": {"words": [1] * 5}}
    monkeypatch.setattr(provider_module, "is_trial_target", lambda: True)
    monkeypatch.setattr(provider, "get_players", list)
    monkeypatch.setattr(provider_module, "installer_provenance", lambda: provenance)
    monkeypatch.setattr(trial_binary, "EXPECTED_SHA", "a" * 64)
    monkeypatch.setattr(
        trial_binary,
        "attest_owned_child",
        AsyncMock(return_value={"role": "ptp", "outcome": "match"}),
    )
    response = await provider.get_private_native_volume_capture_readiness()

    assert response == {
        "installer_provenance": provenance,
        "expected_sha256": {"encoding": "u32be-v1", "words": [0xAAAAAAAA] * 8},
        "startup_validation": {"check_outcome": "passed"},
        "owned_children": [{"role": "ptp", "outcome": "match"}],
    }


async def test_retains_sealed_capture_after_stream_reference_clears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The provider keeps the historical capture after its owning stream drops it."""
    provider, _ = _provider()
    monkeypatch.setattr(provider_module, "is_trial_target", lambda: True)
    stream = MagicMock()
    capture = stream.native_volume_capture = PrivateVolumeCapture()
    capture.end(None, "stream-cleared")
    provider.record_private_capture(capture)
    stream.native_volume_capture = None

    response = await provider.get_private_native_volume_capture()

    record = response["records"][0]
    assert isinstance(record, dict)
    assert record["generation"] == capture.generation


async def test_handler_returns_only_detached_bounded_historical_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route retains sealed records after streams clear and returns no live data."""
    provider, _ = _provider()
    monkeypatch.setattr(provider_module, "is_trial_target", lambda: True)
    captures = [PrivateVolumeCapture() for _ in range(17)]
    for capture in captures:
        provider.record_private_capture(capture)

    response = await provider.get_private_native_volume_capture()

    assert set(response) == {"records"}
    assert len(response["records"]) == 16
    record = response["records"][0]
    assert isinstance(record, dict)
    assert record["generation"] == captures[1].generation
    counters = record["dacp_counters"]
    assert isinstance(counters, dict)
    counters["volume"] = 999
    original_counters = captures[1].diagnostics()["dacp_counters"]
    assert isinstance(original_counters, dict)
    assert original_counters["volume"] == 0


async def test_unload_unregisters_private_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provider unload releases the fixed command name for a later reload."""
    provider, _ = _provider()
    unregister = MagicMock()
    unregister_readiness = MagicMock()
    provider._unregister_private_capture_api = unregister
    provider._unregister_private_capture_readiness_api = unregister_readiness
    provider._ptp_daemon_ready = None
    provider._ptp_daemon_stdout_task = None
    provider._ptp_daemon = None
    provider._dacp_server = MagicMock()
    provider._dacp_server.__bool__.return_value = False
    provider._dacp_info = MagicMock()
    provider._dacp_info.__bool__.return_value = False
    monkeypatch.setattr(provider_module, "is_trial_target", lambda: False)

    await provider.unload()

    unregister.assert_called_once_with()
    unregister_readiness.assert_called_once_with()
    assert provider._unregister_private_capture_api is None
