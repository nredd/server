"""Tests for the private guarded AirPlay flag inspect and compare-and-set route."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.auth import Scope
from music_assistant_models.config_entries import ConfigEntry, CoreConfig, ProviderConfig
from music_assistant_models.enums import ConfigEntryType

from music_assistant.controllers.config import ConfigController
from music_assistant.helpers.api import APICommandHandler, parse_arguments
from music_assistant.helpers.private_trial import CONF_PRIVATE_TRIAL_TARGET
from music_assistant.mass import MusicAssistant
from music_assistant.providers.airplay import provider as provider_module
from music_assistant.providers.airplay.constants import (
    CONF_EXPERIMENTAL_HOMEPOD_REMOTE,
    CONF_EXPERIMENTAL_SESSION_VOLUME,
    CONF_PRIVATE_NATIVE_VOLUME_CAPTURE,
)
from music_assistant.providers.airplay.provider import AirPlayProvider

INSTANCE = "airplay--abc123"
FLAGS = (
    CONF_EXPERIMENTAL_HOMEPOD_REMOTE,
    CONF_EXPERIMENTAL_SESSION_VOLUME,
    CONF_PRIVATE_NATIVE_VOLUME_CAPTURE,
)
SECRET = "SENTINEL-credential-9f3a"
OTHER_ID = "airplay_receiver--zzz999"
RESPONSE_KEYS = {
    "version",
    "status",
    "reason",
    "before",
    "after",
    "memory",
    "persistence",
    "effect",
}
ABSENT = dict.fromkeys(FLAGS, "absent")


def _raw_provider() -> dict[str, Any]:
    """Return a raw instance holding noncanonical neighbours and a credential sentinel."""
    return {
        "type": "player",
        "domain": "airplay",
        "instance_id": INSTANCE,
        "enabled": True,
        "name": "  Odd   Name ",
        "last_error": {"junk": [1, 2, None]},
        "unknown_top_level": {"keep": ["me", 3]},
        "setup_data": {"native_mrp_credentials": SECRET},
        "values": {
            "log_level": "WEIRD",
            "streaming_mode": 7,
            "password": SECRET,
            "unknown_value": {"nested": [None, "x"]},
            "radio_station_first": "not-a-bool",
        },
    }


def _config_tree() -> dict[str, Any]:
    """Return the raw sections the guards and the neighbours live in."""
    return {
        "providers": {
            INSTANCE: _raw_provider(),
            OTHER_ID: {"domain": "airplay_receiver", "values": {"password": SECRET}},
            "plain": {"domain": "filesystem_local", "values": "broken"},
        },
        "players": {"ap0123456789ab": {"values": {"password": SECRET, "volume_control": 3}}},
        "core": {"discovery": {"values": {CONF_PRIVATE_TRIAL_TARGET: ""}}},
    }


def _request(expected: dict[str, str], desired: dict[str, str]) -> dict[str, Any]:
    """Build a compare-and-set request."""
    return {"mode": "compare_and_set", "expected": expected, "desired": desired}


def _states(**changes: str) -> dict[str, str]:
    """Return all-absent flag states with overrides keyed by the flag key."""
    return {**ABSENT, **changes}


class Harness:
    """A real config controller on disk with one provider wired in."""

    def __init__(
        self, mass: MusicAssistant, provider: AirPlayProvider, patch: pytest.MonkeyPatch
    ) -> None:
        """Remember the real controller, the provider under test and the patcher."""
        self.mass = mass
        self.provider = provider
        self.patch = patch
        self.signal = MagicMock()
        self.load = MagicMock()
        self.unload = MagicMock()
        self.players = MagicMock(return_value=[])
        self.logger = MagicMock()

    @property
    def config(self) -> ConfigController:
        """Return the real config controller."""
        return self.mass.config

    @property
    def raw(self) -> dict[str, Any]:
        """Return the live raw tree."""
        return self.config._data

    def disk(self) -> dict[str, Any]:
        """Return what the settings file currently holds."""
        return cast(
            "dict[str, Any]", json.loads(Path(self.config.filename).read_text(encoding="utf-8"))
        )

    def values(self) -> dict[str, Any]:
        """Return the live raw values mapping of the instance."""
        return cast("dict[str, Any]", self.raw["providers"][INSTANCE]["values"])


@pytest.fixture
async def harness(mass_minimal: MusicAssistant, monkeypatch: pytest.MonkeyPatch) -> Harness:
    """Build a loaded-looking provider over real config storage and a real file."""
    mass = mass_minimal
    monkeypatch.setattr(provider_module, "is_trial_target", lambda: True)
    for section, value in _config_tree().items():
        mass.config.set(section, value)
    await mass.config.async_save()
    entries = [
        ConfigEntry(key=key, type=ConfigEntryType.BOOLEAN, default_value=False, required=False)
        for key in FLAGS
    ]
    provider = AirPlayProvider.__new__(AirPlayProvider)
    provider.mass = mass
    provider.__dict__["manifest"] = SimpleNamespace(domain="airplay")
    loadable = {
        key: value
        for key, value in mass.config.get(f"providers/{INSTANCE}").items()
        if key != "last_error"
    }
    provider.__dict__["config"] = ProviderConfig.parse(entries, loadable)
    provider.available = True
    mass._providers[INSTANCE] = provider
    target = ConfigEntry(key=CONF_PRIVATE_TRIAL_TARGET, type=ConfigEntryType.STRING, value="")
    monkeypatch.setattr(
        mass.discovery,
        "config",
        CoreConfig.parse([target], {"domain": "discovery", "values": {}}),
        raising=False,
    )
    built = Harness(mass, provider, monkeypatch)
    provider.__dict__["get_players"] = built.players
    provider.__dict__["logger"] = built.logger
    monkeypatch.setattr(mass, "signal_event", built.signal)
    monkeypatch.setattr(mass, "load_provider_config", built.load)
    monkeypatch.setattr(mass, "unload_provider", built.unload)
    return built


def _spy_flush(harness: Harness) -> AsyncMock:
    """Wrap the real flush so calls are observable while the file is still written."""
    real = harness.config.async_save
    spy = AsyncMock(side_effect=real)
    harness.patch.setattr(harness.config, "async_save", spy)
    return spy


def _assert_no_side_effects(harness: Harness) -> None:
    """Assert nothing reloaded, signalled or controlled."""
    harness.signal.assert_not_called()
    harness.load.assert_not_called()
    harness.unload.assert_not_called()
    harness.players.assert_called()


def _loaded_snapshot(harness: Harness) -> dict[str, Any]:
    """Return the loaded provider config as a comparable structure."""
    return harness.provider.config.to_raw()


def _expected_tree(before: dict[str, Any], **leaves: str) -> dict[str, Any]:
    """Return the tree with only the named flag leaves changed or removed."""
    tree = deepcopy(before)
    values = tree["providers"][INSTANCE]["values"]
    for key, state in leaves.items():
        if state == "absent":
            values.pop(key, None)
        else:
            values[key] = state == "true"
    return tree


async def test_inspect_is_read_only_and_closed(harness: Harness) -> None:
    """Inspect returns closed prior states and never flushes or mutates."""
    before = deepcopy(harness.raw)
    requested = harness.config._save_requested
    flush = _spy_flush(harness)
    result = await harness.provider.private_airplay_flags({"mode": "inspect"})
    assert result == {
        "version": 1,
        "status": "inspected",
        "reason": None,
        "before": ABSENT,
        "after": None,
        "memory": "unchanged",
        "persistence": "not_attempted",
        "effect": "none",
    }
    assert harness.raw == before
    assert harness.config._save_requested == requested
    flush.assert_not_called()
    _assert_no_side_effects(harness)


async def test_inspect_reports_each_stored_state(harness: Harness) -> None:
    """Absent, true and false are distinct closed states."""
    harness.values().update(
        {CONF_EXPERIMENTAL_HOMEPOD_REMOTE: True, CONF_EXPERIMENTAL_SESSION_VOLUME: False}
    )
    result = await harness.provider.private_airplay_flags({"mode": "inspect"})
    assert result["before"] == {
        CONF_EXPERIMENTAL_HOMEPOD_REMOTE: "true",
        CONF_EXPERIMENTAL_SESSION_VOLUME: "false",
        CONF_PRIVATE_NATIVE_VOLUME_CAPTURE: "absent",
    }


async def test_cas_changes_only_the_three_leaves_and_roundtrips(harness: Harness) -> None:
    """Every transition keeps unrelated raw data identical, in memory and on disk."""
    original = deepcopy(harness.raw)
    loaded = _loaded_snapshot(harness)
    current = dict(ABSENT)
    path = [
        {FLAGS[0]: "true", FLAGS[1]: "false", FLAGS[2]: "true"},
        {FLAGS[0]: "false", FLAGS[1]: "true", FLAGS[2]: "false"},
        {FLAGS[0]: "absent", FLAGS[1]: "absent", FLAGS[2]: "absent"},
    ]
    for desired in path:
        before = deepcopy(harness.raw)
        result = await harness.provider.private_airplay_flags(_request(current, desired))
        assert result == {
            "version": 1,
            "status": "applied",
            "reason": None,
            "before": current,
            "after": desired,
            "memory": "applied",
            "persistence": "completed",
            "effect": "next_load",
        }
        assert harness.raw == _expected_tree(before, **desired)
        assert harness.disk() == harness.raw
        assert _loaded_snapshot(harness) == loaded
        current = desired
    assert harness.raw == original
    assert harness.disk() == original
    _assert_no_side_effects(harness)


async def test_inverse_cas_restores_original_absence_not_a_default(harness: Harness) -> None:
    """An inverse CAS removes the key instead of writing a guessed false."""
    forward = _states(**{FLAGS[2]: "true"})
    await harness.provider.private_airplay_flags(_request(ABSENT, forward))
    assert harness.values()[FLAGS[2]] is True
    await harness.provider.private_airplay_flags(_request(forward, ABSENT))
    assert FLAGS[2] not in harness.values()
    assert FLAGS[2] not in harness.disk()["providers"][INSTANCE]["values"]


async def test_only_differing_leaves_are_touched(harness: Harness) -> None:
    """Unchanged flags keep their exact stored form, including an explicit false."""
    harness.values()[FLAGS[0]] = False
    await harness.provider.private_airplay_flags(
        _request(_states(**{FLAGS[0]: "false"}), _states(**{FLAGS[0]: "false", FLAGS[1]: "true"}))
    )
    assert harness.values()[FLAGS[0]] is False
    assert harness.values()[FLAGS[1]] is True


async def test_noop_cas_does_not_flush_or_mutate(harness: Harness) -> None:
    """Expected equal to desired and current is a pure no-op."""
    before = deepcopy(harness.raw)
    requested = harness.config._save_requested
    flush = _spy_flush(harness)
    result = await harness.provider.private_airplay_flags(_request(ABSENT, ABSENT))
    assert result["status"] == "unchanged"
    assert result["persistence"] == "not_attempted"
    assert result["effect"] == "none"
    assert harness.raw == before
    assert harness.config._save_requested == requested
    flush.assert_not_called()


async def test_expected_mismatch_writes_nothing(harness: Harness) -> None:
    """A stale expectation is refused, reporting only the closed current states."""
    before = deepcopy(harness.raw)
    flush = _spy_flush(harness)
    result = await harness.provider.private_airplay_flags(
        _request(_states(**{FLAGS[0]: "true"}), _states(**{FLAGS[0]: "false"}))
    )
    assert result["status"] == "refused"
    assert result["reason"] == "state_mismatch"
    assert result["before"] == ABSENT
    assert result["memory"] == "unchanged"
    assert harness.raw == before
    flush.assert_not_called()


def _mutate(harness: Harness, change: Callable[[dict[str, Any]], None]) -> None:
    """Apply a raw-tree change."""
    change(harness.raw)


def _drop_values(raw: dict[str, Any]) -> None:
    del raw["providers"][INSTANCE]["values"]


GUARD_CASES: dict[str, tuple[Callable[[Harness], object], str]] = {
    "values_missing": (lambda h: _mutate(h, _drop_values), "unsafe_config"),
    "values_not_mapping": (
        lambda h: h.raw["providers"][INSTANCE].update(values=[]),
        "unsafe_config",
    ),
    "stored_null": (lambda h: h.values().update({FLAGS[0]: None}), "unsafe_config"),
    "stored_string": (lambda h: h.values().update({FLAGS[1]: "true"}), "unsafe_config"),
    "stored_int": (lambda h: h.values().update({FLAGS[2]: 1}), "unsafe_config"),
    "stored_zero": (lambda h: h.values().update({FLAGS[2]: 0}), "unsafe_config"),
    "top_level_duplicate": (
        lambda h: h.raw["providers"][INSTANCE].update({FLAGS[1]: True}),
        "unsafe_config",
    ),
    "providers_not_mapping": (lambda h: h.raw.update(providers=[]), "unsafe_config"),
    "sibling_null": (lambda h: h.raw["providers"].update(sibling=None), "unsafe_config"),
    "sibling_list": (lambda h: h.raw["providers"].update(sibling=[]), "unsafe_config"),
    "sibling_domain_missing": (lambda h: h.raw["providers"].update(sibling={}), "unsafe_config"),
    "sibling_domain_null": (
        lambda h: h.raw["providers"].update(sibling={"domain": None}),
        "unsafe_config",
    ),
    "sibling_domain_list": (
        lambda h: h.raw["providers"].update(sibling={"domain": []}),
        "unsafe_config",
    ),
    "second_airplay_instance": (
        lambda h: h.raw["providers"].update({"airplay--two": {"domain": "airplay"}}),
        "unsafe_config",
    ),
    "raw_instance_id_mismatch": (
        lambda h: h.raw["providers"][INSTANCE].update(instance_id="airplay--other"),
        "unsafe_config",
    ),
    "raw_instance_id_missing": (
        lambda h: h.raw["providers"][INSTANCE].pop("instance_id"),
        "unsafe_config",
    ),
    "raw_domain_changed": (
        lambda h: h.raw["providers"][INSTANCE].update(domain="other"),
        "unsafe_config",
    ),
    "provider_unavailable": (
        lambda h: setattr(h.provider, "available", False),
        "provider_unavailable",
    ),
    "provider_not_registered": (
        lambda h: h.mass._providers.pop(INSTANCE),
        "provider_unavailable",
    ),
    "provider_replaced": (
        lambda h: h.mass._providers.update({INSTANCE: MagicMock()}),
        "provider_unavailable",
    ),
    "wrong_domain": (
        lambda h: setattr(h.provider, "manifest", SimpleNamespace(domain="other")),
        "provider_unavailable",
    ),
    "target_raw_missing": (
        lambda h: h.raw["core"]["discovery"]["values"].pop(CONF_PRIVATE_TRIAL_TARGET),
        "target_not_empty",
    ),
    "target_raw_null": (
        lambda h: h.raw["core"]["discovery"]["values"].update({CONF_PRIVATE_TRIAL_TARGET: None}),
        "target_not_empty",
    ),
    "target_raw_set": (
        lambda h: h.raw["core"]["discovery"]["values"].update(
            {CONF_PRIVATE_TRIAL_TARGET: "0123456789ab@Name"}
        ),
        "target_not_empty",
    ),
    "target_loaded_set": (
        lambda h: setattr(
            h.mass.discovery.config.values[CONF_PRIVATE_TRIAL_TARGET], "value", "0123456789ab@X"
        ),
        "target_not_empty",
    ),
    "core_missing": (lambda h: h.raw.pop("core"), "target_not_empty"),
    "player_present": (
        lambda h: setattr(h.players, "return_value", [MagicMock()]),
        "players_active",
    ),
}


@pytest.mark.parametrize("case", sorted(GUARD_CASES))
@pytest.mark.parametrize("mode", ["inspect", "compare_and_set"])
async def test_every_guard_fails_without_writing(harness: Harness, case: str, mode: str) -> None:
    """Each guard refuses with a fixed reason and performs no write or flush."""
    arrange, reason = GUARD_CASES[case]
    arrange(harness)
    before = deepcopy(harness.raw)
    requested = harness.config._save_requested
    flush = _spy_flush(harness)
    request = (
        {"mode": "inspect"}
        if mode == "inspect"
        else _request(ABSENT, _states(**{FLAGS[2]: "true"}))
    )
    result = await harness.provider.private_airplay_flags(request)
    assert result == {
        "version": 1,
        "status": "refused",
        "reason": reason,
        "before": None,
        "after": None,
        "memory": "unchanged",
        "persistence": "not_attempted",
        "effect": "none",
    }
    assert harness.raw == before
    assert harness.config._save_requested == requested
    flush.assert_not_called()
    assert INSTANCE not in json.dumps(result)


async def test_profile_inactive_refuses(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    """Outside the private Linux ARM profile the route refuses and writes nothing."""
    monkeypatch.setattr(provider_module, "is_trial_target", lambda: False)
    before = deepcopy(harness.raw)
    result = await harness.provider.private_airplay_flags(
        _request(ABSENT, _states(**{FLAGS[0]: "true"}))
    )
    assert result["reason"] == "profile_inactive"
    assert harness.raw == before


def _bad_requests() -> list[object]:
    """Return malformed requests of every shape."""
    good = _request(ABSENT, _states(**{FLAGS[0]: "true"}))
    return [
        None,
        [],
        "inspect",
        {},
        {"mode": "INSPECT"},
        {"mode": "inspect", "expected": ABSENT},
        {"mode": "inspect", "extra": 1},
        {"mode": "compare_and_set"},
        {"mode": "compare_and_set", "expected": ABSENT},
        {**good, "extra": True},
        {**good, "mode": "set"},
        {**good, "expected": None},
        {**good, "desired": []},
        {**good, "expected": {**ABSENT, "unknown": "absent"}},
        {**good, "desired": {FLAGS[0]: "true"}},
        {**good, "desired": {**ABSENT, FLAGS[0]: True}},
        {**good, "desired": {**ABSENT, FLAGS[0]: None}},
        {**good, "desired": {**ABSENT, FLAGS[0]: "TRUE"}},
        {**good, "desired": {**ABSENT, FLAGS[0]: 1}},
        {**good, "expected": {**ABSENT, FLAGS[0]: ["absent"]}},
    ]


@pytest.mark.parametrize("request_", _bad_requests())
async def test_malformed_requests_are_refused_without_writes(
    harness: Harness, request_: object
) -> None:
    """Unexpected members, wrong types and non-enum states are all refused."""
    before = deepcopy(harness.raw)
    flush = _spy_flush(harness)
    result = await harness.provider.private_airplay_flags(cast("dict[str, Any]", request_))
    assert result["status"] == "refused"
    assert result["reason"] == "invalid_request"
    assert harness.raw == before
    flush.assert_not_called()


@pytest.mark.parametrize("raw_args", [{"request": "x"}, {"request": []}, {"request": None}, {}])
async def test_parser_refuses_malformed_requests_without_writes(
    harness: Harness, raw_args: dict[str, Any]
) -> None:
    """Malformed and missing requests pass through the parser to the closed refusal."""
    before = deepcopy(harness.raw)
    flush = _spy_flush(harness)
    handler = APICommandHandler.parse(
        "airplay/private_flags", harness.provider.private_airplay_flags
    )
    args = parse_arguments(handler.signature, handler.type_hints, raw_args)
    result = await cast("Awaitable[dict[str, Any]]", handler.target(**args))
    assert result["status"] == "refused"
    assert result["reason"] == "invalid_request"
    assert harness.raw == before
    flush.assert_not_called()


async def test_registered_handler_receives_the_request_object_intact(harness: Harness) -> None:
    """The real API parser passes the closed request through untouched."""
    handler = APICommandHandler.parse(
        "airplay/private_flags", harness.provider.private_airplay_flags
    )
    request = _request(ABSENT, _states(**{FLAGS[0]: "true"}))
    args = parse_arguments(handler.signature, handler.type_hints, {"request": request})
    assert args == {"request": request}
    result = await cast("Awaitable[dict[str, Any]]", handler.target(**args))
    assert result["status"] == "applied"


async def test_flush_failure_reports_unproven_and_keeps_memory(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed flush leaves memory applied and is never reported as complete."""
    monkeypatch.setattr(harness.config, "async_save", AsyncMock(side_effect=OSError(SECRET)))
    desired = _states(**{FLAGS[0]: "true"})
    result = await harness.provider.private_airplay_flags(_request(ABSENT, desired))
    assert result["status"] == "applied"
    assert result["memory"] == "applied"
    assert result["persistence"] == "unproven"
    assert result["effect"] == "next_load"
    assert result["after"] == desired
    assert harness.values()[FLAGS[0]] is True
    assert SECRET not in json.dumps(result)
    assert SECRET not in str(harness.logger.warning.call_args_list)


async def test_cancelled_flush_propagates_and_is_not_success(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation during the flush is raised, with the applied memory left truthful."""
    monkeypatch.setattr(harness.config, "async_save", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await harness.provider.private_airplay_flags(
            _request(ABSENT, _states(**{FLAGS[0]: "true"}))
        )
    assert harness.values()[FLAGS[0]] is True
    harness.logger.warning.assert_called_once()


async def test_cancelled_task_is_cancelled(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task cancelled mid flush ends cancelled instead of returning a result."""
    gate = asyncio.Event()

    async def slow() -> None:
        await gate.wait()

    monkeypatch.setattr(harness.config, "async_save", slow)
    task = asyncio.create_task(
        harness.provider.private_airplay_flags(_request(ABSENT, _states(**{FLAGS[0]: "true"})))
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


async def test_concurrent_writer_during_flush_is_not_clobbered(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A writer running during the flush keeps its value and persistence is unproven."""
    real = harness.config.async_save

    async def concurrent() -> None:
        harness.values()[FLAGS[1]] = True
        harness.values()[FLAGS[0]] = False
        harness.values()["concurrent_neighbour"] = "kept"
        await real()

    monkeypatch.setattr(harness.config, "async_save", concurrent)
    desired = _states(**{FLAGS[0]: "true"})
    result = await harness.provider.private_airplay_flags(_request(ABSENT, desired))
    assert result["memory"] == "applied"
    assert result["persistence"] == "unproven"
    assert result["after"] == {
        FLAGS[0]: "false",
        FLAGS[1]: "true",
        FLAGS[2]: "absent",
    }
    assert harness.values()["concurrent_neighbour"] == "kept"
    assert harness.values()[FLAGS[1]] is True


async def test_concurrent_writer_invalidating_shape_is_unproven(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the stored shape becomes unusable during the flush there is no after state."""
    real = harness.config.async_save

    async def concurrent() -> None:
        harness.values()[FLAGS[2]] = None
        await real()

    monkeypatch.setattr(harness.config, "async_save", concurrent)
    result = await harness.provider.private_airplay_flags(
        _request(ABSENT, _states(**{FLAGS[0]: "true"}))
    )
    assert result["after"] is None
    assert result["persistence"] == "unproven"


async def test_no_await_between_guards_and_leaf_writes(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No event loop turn may run between the guard snapshot and the first write."""
    ticks = 0
    stop = False

    async def ticker() -> None:
        nonlocal ticks
        while not stop:
            ticks += 1
            await asyncio.sleep(0)

    snapshot_ticks: list[int] = []
    write_ticks: list[int] = []
    real_snapshot = harness.provider._private_flag_snapshot
    real_set, real_remove = harness.config.set, harness.config.remove

    def snapshot() -> Any:
        snapshot_ticks.append(ticks)
        return real_snapshot()

    def record_set(*args: Any, **kwargs: Any) -> None:
        write_ticks.append(ticks)
        real_set(*args, **kwargs)

    def record_remove(*args: Any, **kwargs: Any) -> None:
        write_ticks.append(ticks)
        real_remove(*args, **kwargs)

    monkeypatch.setattr(harness.provider, "_private_flag_snapshot", snapshot)
    monkeypatch.setattr(harness.config, "set", record_set)
    monkeypatch.setattr(harness.config, "remove", record_remove)
    background = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    try:
        await harness.provider.private_airplay_flags(
            _request(ABSENT, _states(**{FLAGS[0]: "true", FLAGS[1]: "false"}))
        )
        await harness.provider.private_airplay_flags(
            _request(_states(**{FLAGS[0]: "true", FLAGS[1]: "false"}), ABSENT)
        )
    finally:
        stop = True
        await background
    assert len(write_ticks) == 4
    assert write_ticks[:2] == [snapshot_ticks[0]] * 2
    assert write_ticks[2:] == [snapshot_ticks[1]] * 2


async def test_generic_provider_writers_are_never_used(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither the normalizing save path nor the loaded-cache writer is touched."""
    for name in ("set_raw_provider_config_value", "save_provider_config", "get_provider_config"):
        monkeypatch.setattr(harness.config, name, MagicMock(side_effect=AssertionError(name)))
    result = await harness.provider.private_airplay_flags(
        _request(ABSENT, _states(**{FLAGS[0]: "true"}))
    )
    assert result["status"] == "applied"
    assert harness.provider.config.values[FLAGS[0]].value is False
    assert (
        harness.provider.config.values[FLAGS[0]].value
        == harness.provider.config.values[FLAGS[0]].default_value
    )


async def test_next_load_rehydrates_from_disk(harness: Harness) -> None:
    """A fresh controller on the same file loads the desired flags and the same neighbours."""
    desired = {FLAGS[0]: "true", FLAGS[1]: "false", FLAGS[2]: "true"}
    await harness.provider.private_airplay_flags(_request(ABSENT, desired))
    harness.patch.setattr(harness.mass, "register_api_command", MagicMock())
    reloaded = ConfigController(harness.mass)
    await reloaded.setup()
    raw = reloaded.get(f"providers/{INSTANCE}")
    assert raw == harness.raw["providers"][INSTANCE]
    entries = [
        ConfigEntry(key=key, type=ConfigEntryType.BOOLEAN, default_value=False, required=False)
        for key in FLAGS
    ]
    config = ProviderConfig.parse(entries, {k: v for k, v in raw.items() if k != "last_error"})
    assert [config.values[key].value for key in FLAGS] == [True, False, True]
    assert reloaded.get("providers/plain/values") == "broken"


async def test_responses_are_closed_and_secret_free(harness: Harness) -> None:
    """Every response has exactly the closed keys and leaks no identifier or secret."""
    responses = [
        await harness.provider.private_airplay_flags({"mode": "inspect"}),
        await harness.provider.private_airplay_flags(
            _request(ABSENT, _states(**{FLAGS[0]: "true"}))
        ),
        await harness.provider.private_airplay_flags({"mode": "nope"}),
    ]
    for response in responses:
        assert set(response) == RESPONSE_KEYS
        text = json.dumps(response)
        for forbidden in (SECRET, INSTANCE, "ap0123456789ab", "Odd"):
            assert forbidden not in text


async def test_route_registration_is_system_manage_scoped(harness: Harness) -> None:
    """The route registers on the real registry with the system management scope."""
    registry = MusicAssistant.__new__(MusicAssistant)
    registry.command_handlers = {}
    harness.patch.setattr(harness.mass, "register_api_command", registry.register_api_command)
    harness.provider._register_private_capture_api()
    handler = registry.command_handlers["airplay/private_flags"]
    assert handler.required_scope == Scope.SYSTEM_MANAGE
    assert handler.target == harness.provider.private_airplay_flags
    unregister = harness.provider._unregister_private_flags_api
    assert unregister is not None
    unregister()
    assert "airplay/private_flags" not in registry.command_handlers
