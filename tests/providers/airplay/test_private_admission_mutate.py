"""
Tests for the private atomic admission mutation.

References:
    https://github.com/nredd/server/issues/73
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Awaitable
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypedDict, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.streams import StreamReader
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.auth import Scope
from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
from music_assistant_models.enums import ConfigEntryType

from music_assistant.controllers.config import ConfigController
from music_assistant.controllers.discovery import controller as discovery_module
from music_assistant.controllers.players import PlayerController
from music_assistant.controllers.webserver import controller as webserver_module
from music_assistant.controllers.webserver.controller import WebserverController
from music_assistant.helpers.api import APICommandHandler, parse_arguments
from music_assistant.helpers.json import json_dumps
from music_assistant.helpers.private_trial import CONF_PRIVATE_TRIAL_TARGET
from music_assistant.mass import MusicAssistant
from music_assistant.providers.airplay import provider as provider_module
from music_assistant.providers.airplay import trial_binary
from music_assistant.providers.airplay.constants import (
    CONF_COMPANION_CREDENTIALS,
    CONF_MRP_CREDENTIALS,
    CONF_NATIVE_MRP_CREDENTIALS,
    CONF_PRIVATE_NATIVE_VOLUME_CAPTURE,
    MRP_DISCOVERY_TYPE,
)
from music_assistant.providers.airplay.provider import AirPlayProvider
from tests.providers.airplay.test_control_player import _make_control_player, _service_info

INSTANCE = "airplay--abc123"
PLAYER = "ap0123456789ab"
TARGET = "0123456789ab@Example TV"


@pytest.fixture
async def admission(
    mass_minimal: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> AirPlayProvider:
    """Wire a real config and discovery config to a loaded AirPlay provider."""
    monkeypatch.setattr(provider_module, "is_trial_target", lambda: True)
    raw = {
        "domain": "airplay",
        "instance_id": INSTANCE,
        "enabled": True,
        "type": "player",
        "values": {},
    }
    mass_minimal.config.set("providers", {INSTANCE: raw})
    mass_minimal.config.set("players", {})
    mass_minimal.config.set(
        "core",
        {
            "discovery": {
                "domain": "discovery",
                "values": {
                    CONF_PRIVATE_TRIAL_TARGET: "",
                    "unknown": {"keep": [1, None]},
                },
                "unknown": 0,
            }
        },
    )
    entries = [
        ConfigEntry(key=key, type=ConfigEntryType.BOOLEAN, default_value=False)
        for key in provider_module._PRIVATE_FLAG_KEYS
    ]
    provider = AirPlayProvider.__new__(AirPlayProvider)
    provider.mass = mass_minimal
    provider.__dict__["manifest"] = SimpleNamespace(domain="airplay", name="AirPlay")
    provider.__dict__["config"] = ProviderConfig.parse(entries, raw)
    provider.__dict__["get_players"] = MagicMock(return_value=[])
    provider.__dict__["logger"] = MagicMock()
    provider.available = True
    provider.unloading = False
    mass_minimal._providers[INSTANCE] = provider
    monkeypatch.setattr(discovery_module, "is_trial_target", lambda: True)
    monkeypatch.setattr(mass_minimal.discovery, "_setup_mdns_browser", AsyncMock())
    monkeypatch.setattr(mass_minimal.discovery, "_register_mass_service", AsyncMock())
    monkeypatch.setattr(mass_minimal.discovery, "_schedule_periodic_upnp_discovery", lambda: None)
    await mass_minimal.discovery.setup(await mass_minimal.config.get_core_config("discovery"))
    await mass_minimal.config.async_save()
    return provider


class AdmissionRequest(TypedDict):
    """Closed request for the mutation route."""

    mode: str
    player_id: str
    target: str


def request(mode: str = "admit") -> AdmissionRequest:
    """Return a valid closed request."""
    return {"mode": mode, "player_id": PLAYER, "target": TARGET}


async def test_admit_and_clear_preserve_raw_neighbours(admission: AirPlayProvider) -> None:
    """Only the target leaf changes, including when clearing an active player."""
    cfg = admission.mass.config
    before = deepcopy(cfg._data)
    result = await admission.private_trial_admission_mutate(request())
    assert result == {
        "version": 1,
        "status": "applied",
        "reason": None,
        "memory": "applied",
        "persistence": "completed",
    }
    before["core"]["discovery"]["values"][CONF_PRIVATE_TRIAL_TARGET] = TARGET
    assert cfg._data == before
    assert admission.mass.discovery.config.values[CONF_PRIVATE_TRIAL_TARGET].value == TARGET
    admission.__dict__["get_players"].return_value = [
        SimpleNamespace(player_id=PLAYER, stream=object())
    ]
    result = await admission.private_trial_admission_mutate(request("clear"))
    assert result["status"] == "applied"
    before["core"]["discovery"]["values"][CONF_PRIVATE_TRIAL_TARGET] = ""
    assert cfg._data == before
    assert admission.mass.discovery.config.values[CONF_PRIVATE_TRIAL_TARGET].value == ""


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        {},
        {"mode": "admit", "player_id": PLAYER, "target": TARGET, "extra": 1},
        {"mode": "admit", "player_id": PLAYER, "target": "0123456789ab@bad/name"},
        {"mode": "admit", "player_id": "apabcdefabcdef", "target": TARGET},
        {"mode": True, "player_id": PLAYER, "target": TARGET},
    ],
)
async def test_invalid_refuses_without_flush(
    admission: AirPlayProvider, monkeypatch: pytest.MonkeyPatch, invalid: Any
) -> None:
    """Reject malformed requests without writes or flushes."""
    before = deepcopy(admission.mass.config._data)
    flush = AsyncMock()
    monkeypatch.setattr(admission.mass.config, "async_save", flush)
    result = await admission.private_trial_admission_mutate(invalid)
    assert result["status"] == "refused"
    assert admission.mass.config._data == before
    flush.assert_not_awaited()


@pytest.mark.parametrize(
    "state",
    [
        "missing",
        "null",
        "setup",
        "bounds",
        "credential",
        "flag",
        "player",
        "raw_target",
        "loaded_target",
    ],
)
async def test_unsafe_state_refuses(
    admission: AirPlayProvider, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    """Unsafe admission states cannot write or flush."""
    cfg = admission.mass.config
    if state == "missing":
        del cfg._data["players"]
    elif state == "null":
        cfg._data["players"] = None
    elif state in ("setup", "bounds", "credential"):
        cfg._data["players"][PLAYER] = (
            {"setup_data": {"x": 1}}
            if state == "setup"
            else {"values": {"min_volume": 1} if state == "bounds" else {"password": "secret"}}
        )
    elif state == "flag":
        admission.config.values[CONF_PRIVATE_NATIVE_VOLUME_CAPTURE].value = True
    elif state == "player":
        admission.__dict__["get_players"].return_value = [SimpleNamespace(stream=None)]
    elif state == "raw_target":
        del cfg._data["core"]["discovery"]["values"][CONF_PRIVATE_TRIAL_TARGET]
    else:
        admission.mass.discovery.config.values[CONF_PRIVATE_TRIAL_TARGET].value = TARGET
    before = deepcopy(cfg._data)
    flush = AsyncMock()
    monkeypatch.setattr(cfg, "async_save", flush)
    result = await admission.private_trial_admission_mutate(request())
    assert result["status"] == "refused"
    assert cfg._data == before
    flush.assert_not_awaited()


@pytest.mark.parametrize(
    "case",
    [
        "profile",
        "disabled",
        "raw_disabled",
        "wrong_instance",
        "wrong_domain",
        "missing_core",
        "discovery_null",
        "values_null",
        "entry_missing",
        "entry_invalid",
        "nonempty_target",
        "player_null",
        "player_values_null",
        "player_setup_null",
    ],
)
async def test_additional_refusals(
    admission: AirPlayProvider, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """Each malformed or unavailable state fails without mutation or a flush."""
    cfg = admission.mass.config
    if case == "profile":
        monkeypatch.setattr(provider_module, "is_trial_target", lambda: False)
    elif case == "disabled":
        admission.config.enabled = False
    elif case == "raw_disabled":
        cfg._data["providers"][INSTANCE]["enabled"] = False
    elif case == "wrong_instance":
        cfg._data["providers"][INSTANCE]["instance_id"] = "wrong"
    elif case == "wrong_domain":
        cfg._data["providers"][INSTANCE]["domain"] = "wrong"
    elif case == "missing_core":
        del cfg._data["core"]
    elif case == "discovery_null":
        cfg._data["core"]["discovery"] = None
    elif case == "values_null":
        cfg._data["core"]["discovery"]["values"] = None
    elif case == "entry_missing":
        del admission.mass.discovery.config.values[CONF_PRIVATE_TRIAL_TARGET]
    elif case == "entry_invalid":
        cast("Any", admission.mass.discovery.config.values)[CONF_PRIVATE_TRIAL_TARGET] = None
    elif case == "nonempty_target":
        cfg._data["core"]["discovery"]["values"][CONF_PRIVATE_TRIAL_TARGET] = TARGET
    elif case == "player_null":
        cfg._data["players"][PLAYER] = None
    elif case == "player_values_null":
        cfg._data["players"][PLAYER] = {"values": None}
    else:
        cfg._data["players"][PLAYER] = {"setup_data": None}
    before = deepcopy(cfg._data)
    flush = AsyncMock()
    monkeypatch.setattr(cfg, "async_save", flush)
    assert (await admission.private_trial_admission_mutate(request()))["status"] == "refused"
    assert cfg._data == before
    flush.assert_not_awaited()


async def test_clear_mismatch_and_unavailable_refuse(
    admission: AirPlayProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never clear a different target or operate through an unavailable/unloaded provider."""
    assert (await admission.private_trial_admission_mutate(request()))["status"] == "applied"
    cfg = admission.mass.config
    flush = AsyncMock()
    monkeypatch.setattr(cfg, "async_save", flush)
    before = deepcopy(cfg._data)
    wrong = request("clear") | {"target": "0123456789ab@Other"}
    assert (await admission.private_trial_admission_mutate(wrong))["status"] == "refused"
    admission.available = False
    assert (await admission.private_trial_admission_mutate(request("clear")))[
        "reason"
    ] == "provider_unavailable"
    admission.available = True
    admission.unloading = True
    assert (await admission.private_trial_admission_mutate(request("clear")))[
        "reason"
    ] == "provider_unavailable"
    admission.unloading = False
    del admission.mass._providers[INSTANCE]
    assert (await admission.private_trial_admission_mutate(request("clear")))[
        "reason"
    ] == "provider_unavailable"
    assert cfg._data == before
    flush.assert_not_awaited()


async def test_flush_failure_and_cancellation_do_not_roll_back(
    admission: AirPlayProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed or cancelled flush leaves the applied memory state intact."""
    cfg = admission.mass.config
    monkeypatch.setattr(cfg, "async_save", AsyncMock(side_effect=OSError("private")))
    result = await admission.private_trial_admission_mutate(request())
    assert result["persistence"] == "unproven"
    assert cfg._data["core"]["discovery"]["values"][CONF_PRIVATE_TRIAL_TARGET] == TARGET
    monkeypatch.setattr(cfg, "async_save", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await admission.private_trial_admission_mutate(request("clear"))
    assert cfg._data["core"]["discovery"]["values"][CONF_PRIVATE_TRIAL_TARGET] == ""


async def test_queued_writer_runs_only_after_atomic_mutation(
    admission: AirPlayProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A queued writer cannot run between snapshot and target leaf update."""
    cfg = admission.mass.config
    observed: list[str] = []
    loop = asyncio.get_running_loop()

    def writer() -> None:
        observed.append(cfg._data["core"]["discovery"]["values"][CONF_PRIVATE_TRIAL_TARGET])
        cfg.set(f"players/{PLAYER}/setup_data", {"password": "later"})

    loop.call_soon(writer)

    async def flush() -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(cfg, "async_save", flush)
    result = await admission.private_trial_admission_mutate(request())
    assert result["status"] == "applied"
    assert observed == [TARGET]
    assert cfg.get(f"players/{PLAYER}/setup_data") == {"password": "later"}


async def test_old_two_call_preflight_race(admission: AirPlayProvider) -> None:
    """A real core writer can intervene between old preflight and generic target save."""
    cfg = admission.mass.config
    seen = asyncio.Event()
    continue_save = asyncio.Event()

    async def old_client() -> None:
        assert (await admission.get_private_trial_admission_preflight(PLAYER))[
            "target_unset"
        ] is True
        seen.set()
        await continue_save.wait()
        await cfg.save_core_config("discovery", {CONF_PRIVATE_TRIAL_TARGET: TARGET})

    task = asyncio.create_task(old_client())
    await seen.wait()
    await cfg.save_core_config("discovery", {CONF_PRIVATE_TRIAL_TARGET: "ffffffffffff@Other"})
    continue_save.set()
    await task
    assert cfg.get("core/discovery/values/private_trial_target") == TARGET


async def test_later_stale_writer_is_not_serialized(
    admission: AirPlayProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A suspended real generic core save can overwrite the later admission."""
    cfg = admission.mass.config
    entered, resume = asyncio.Event(), asyncio.Event()
    real_get = cfg.get_core_config

    async def stale_get(domain: str) -> Any:
        result = await real_get(domain)
        entered.set()
        await resume.wait()
        return result

    monkeypatch.setattr(cfg, "get_core_config", stale_get)
    monkeypatch.setattr(admission.mass.discovery, "update_config", AsyncMock())
    writer = asyncio.create_task(cfg.save_core_config("discovery", {"upnp_network_scan": True}))
    await entered.wait()
    assert (await admission.private_trial_admission_mutate(request()))["status"] == "applied"
    resume.set()
    await writer
    assert cfg.get("core/discovery/values/private_trial_target") != TARGET
    assert admission.mass.discovery.config.values[CONF_PRIVATE_TRIAL_TARGET].value == TARGET
    assert (await admission.private_trial_admission_mutate(request("clear")))["status"] == "refused"


@pytest.mark.parametrize("key", provider_module._PRIVATE_FLAG_KEYS)
async def test_each_loaded_flag_must_be_false(admission: AirPlayProvider, key: str) -> None:
    """Every loaded experimental flag independently blocks admission."""
    admission.config.values[key].value = True
    assert (await admission.private_trial_admission_mutate(request()))["reason"] == "unsafe_state"


async def test_clear_different_active_player_refuses(admission: AirPlayProvider) -> None:
    """A different active player cannot be silently cleared."""
    assert (await admission.private_trial_admission_mutate(request()))["status"] == "applied"
    admission.__dict__["get_players"].return_value = [
        SimpleNamespace(player_id="apffffffffffff", stream=object())
    ]
    assert (await admission.private_trial_admission_mutate(request("clear")))[
        "reason"
    ] == "unsafe_state"
    assert admission.mass.config.get("core/discovery/values/private_trial_target") == TARGET


async def test_clear_loaded_mismatch_refuses(
    admission: AirPlayProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale loaded entry makes clear a refusal with no writes or flush."""
    assert (await admission.private_trial_admission_mutate(request()))["status"] == "applied"
    cfg = admission.mass.config
    admission.mass.discovery.config.values[CONF_PRIVATE_TRIAL_TARGET].value = "different"
    before = deepcopy(cfg._data)
    flush = AsyncMock()
    monkeypatch.setattr(cfg, "async_save", flush)
    assert (await admission.private_trial_admission_mutate(request("clear")))[
        "reason"
    ] == "target_mismatch"
    assert cfg._data == before
    flush.assert_not_awaited()


async def test_preflight_never_flushes(
    admission: AirPlayProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read-only preflight never starts persistence."""
    flush = AsyncMock()
    monkeypatch.setattr(admission.mass.config, "async_save", flush)
    await admission.get_private_trial_admission_preflight(PLAYER)
    flush.assert_not_awaited()


async def test_cancel_during_real_disk_write(
    admission: AirPlayProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation cannot undo or retry a disk write already running in a thread."""
    cfg = admission.mass.config
    entered, done = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    real_write = cfg._save_to_disk
    writes: list[str] = []

    def delayed_write(data: str) -> None:
        writes.append(data)
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(5):
            raise TimeoutError("test disk gate timed out")
        real_write(data)
        loop.call_soon_threadsafe(done.set)

    monkeypatch.setattr(cfg, "_save_to_disk", delayed_write)
    task = asyncio.create_task(admission.private_trial_admission_mutate(request()))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cfg.get("core/discovery/values/private_trial_target") == TARGET
    finally:
        release.set()
    await asyncio.wait_for(done.wait(), 5)
    assert len(writes) == 1
    stored = json.loads(Path(cfg.filename).read_text(encoding="utf-8"))
    assert stored["core"]["discovery"]["values"][CONF_PRIVATE_TRIAL_TARGET] == TARGET


async def test_lost_response_is_not_retried(
    admission: AirPlayProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discarding a completed response does not cause an implicit second write."""
    cfg = admission.mass.config
    real_save = cfg.async_save
    flush = AsyncMock(side_effect=real_save)
    monkeypatch.setattr(cfg, "async_save", flush)
    await admission.private_trial_admission_mutate(request())
    flush.assert_awaited_once()
    stored = json.loads(Path(cfg.filename).read_text(encoding="utf-8"))
    assert stored["core"]["discovery"]["values"][CONF_PRIVATE_TRIAL_TARGET] == TARGET


@pytest.mark.parametrize("change", ["clear", "unload"])
async def test_change_before_real_construction(
    admission: AirPlayProvider, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """A clear or unload during discovery prevents player construction and its record."""
    mass = admission.mass
    assert (await admission.private_trial_admission_mutate(request()))["status"] == "applied"
    entered, resume = asyncio.Event(), asyncio.Event()

    async def receiver(*_args: object) -> bool:
        entered.set()
        await resume.wait()
        return False

    monkeypatch.setattr(admission, "_is_own_airplay_receiver", receiver)
    monkeypatch.setattr(mass.discovery, "async_find_mdns_service", AsyncMock(return_value=None))
    monkeypatch.setattr(mass, "players", MagicMock(), raising=False)
    monkeypatch.setattr(mass, "streams", MagicMock(publish_ip="192.0.2.2"), raising=False)
    monkeypatch.setattr(provider_module, "get_model_info", lambda _: ("Other", "Receiver"))
    monkeypatch.setattr(
        provider_module,
        "get_primary_ip_address_from_zeroconf",
        lambda *_args, **_kwargs: "192.0.2.1",
    )
    info = MagicMock(type="_raop._tcp.local.", port=0)
    info.parsed_addresses.return_value = ["192.0.2.1"]
    info.decoded_properties = {}
    task = asyncio.create_task(admission._setup_player(PLAYER, "Example TV", info))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if change == "clear":
            assert (await admission.private_trial_admission_mutate(request("clear")))[
                "status"
            ] == "applied"
        else:
            admission.unloading = True
    finally:
        resume.set()
    await task
    assert mass.config.get(f"players/{PLAYER}") is None


@pytest.mark.parametrize("replacement", [False, True])
async def test_clear_during_real_registration_retains_config(
    admission: AirPlayProvider, monkeypatch: pytest.MonkeyPatch, replacement: bool
) -> None:
    """Clear during actual registration removes only the player, retaining its disk config."""
    mass = admission.mass
    assert (await admission.private_trial_admission_mutate(request()))["status"] == "applied"
    controller = PlayerController(mass)
    mass.players = controller
    del admission.__dict__["get_players"]
    admission.unloading = False
    admission._bridge_manager = MagicMock(evaluate_bridge=AsyncMock())
    admission.dashboards = MagicMock()
    monkeypatch.setattr(admission, "_is_own_airplay_receiver", AsyncMock(return_value=False))
    monkeypatch.setattr(mass.discovery, "async_find_mdns_service", AsyncMock(return_value=None))
    monkeypatch.setattr(mass.cache, "get", AsyncMock(return_value=False))
    monkeypatch.setattr(
        mass, "player_queues", MagicMock(on_player_register=AsyncMock()), raising=False
    )
    cast("MagicMock", mass.player_queues).get.return_value = None
    monkeypatch.setattr(mass, "metadata", MagicMock(), raising=False)
    monkeypatch.setattr(mass, "streams", MagicMock(publish_ip="192.0.2.2"), raising=False)
    monkeypatch.setattr(mass, "signal_event", MagicMock())
    monkeypatch.setattr(controller, "_resolve_mac_addresses", AsyncMock())
    monkeypatch.setattr(controller, "_save_underlying_player_id", MagicMock())
    monkeypatch.setattr(controller, "_evaluate_protocol_links", MagicMock())
    monkeypatch.setattr(controller, "_schedule_update_all_players", MagicMock())
    monkeypatch.setattr(provider_module, "get_model_info", lambda _: ("Other", "Receiver"))
    monkeypatch.setattr(
        provider_module,
        "get_primary_ip_address_from_zeroconf",
        lambda *_args, **_kwargs: "192.0.2.1",
    )
    info = MagicMock(type="_raop._tcp.local.", port=0)
    info.parsed_addresses.return_value = ["192.0.2.1"]
    info.decoded_properties = {}
    entered, resume = asyncio.Event(), asyncio.Event()

    async def get_config(player_id: str) -> object:
        entered.set()
        await resume.wait()
        return mass.config.get_base_player_config(player_id, admission.instance_id)

    monkeypatch.setattr(mass.config, "get_player_config", get_config)
    task = asyncio.create_task(admission._setup_player(PLAYER, "Example TV", info))
    successor = MagicMock()
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert controller.get_player(PLAYER) is not None
        assert (await admission.private_trial_admission_mutate(request("clear")))[
            "status"
        ] == "applied"
        if replacement:
            controller._players[PLAYER] = successor
    finally:
        resume.set()
    await task
    assert controller.get_player(PLAYER) is (successor if replacement else None)
    retained = deepcopy(mass.config.get(f"players/{PLAYER}"))
    assert retained
    await mass.config.async_save()
    stored = json.loads(Path(mass.config.filename).read_text(encoding="utf-8"))
    assert stored["players"][PLAYER] == retained


@pytest.mark.parametrize("native", [False, True])
async def test_scheduled_credentialless_tv_and_later_insertion(
    admission: AirPlayProvider, monkeypatch: pytest.MonkeyPatch, native: bool
) -> None:
    """A later credential insertion changes connection reachability outside the contract."""
    assert (await admission.private_trial_admission_mutate(request()))["status"] == "applied"
    monkeypatch.setattr(trial_binary, "is_trial_target", lambda: True)
    player = _make_control_player(setup_data={})
    if native:
        player.mrp_discovery_info = _service_info(MRP_DISCOVERY_TYPE)
    monkeypatch.setattr(
        player,
        "get_setup_value",
        lambda key: admission.mass.config.get(f"players/{PLAYER}/setup_data", {}).get(key),
    )
    build = MagicMock(return_value=None)
    monkeypatch.setattr(player, "_build_config", build)
    await asyncio.create_task(player._connect_control_services())
    assert player.implicit_receiver_writes_blocked
    assert not player.allow_implicit_power_on
    build.assert_not_called()
    admission.mass.config.set(
        f"players/{PLAYER}/setup_data",
        {
            CONF_COMPANION_CREDENTIALS: "later-companion",
            (CONF_NATIVE_MRP_CREDENTIALS if native else CONF_MRP_CREDENTIALS): "later-mrp",
        },
    )
    await asyncio.create_task(player._connect_control_services())
    assert build.call_count == 2


async def test_real_disk_reopen(
    admission: AirPlayProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The explicit target survives a real flush and reload without rewriting neighbours."""
    cfg = admission.mass.config
    expected = deepcopy(cfg._data)
    expected["core"]["discovery"]["values"][CONF_PRIVATE_TRIAL_TARGET] = TARGET
    assert (await admission.private_trial_admission_mutate(request()))["persistence"] == "completed"
    monkeypatch.setattr(admission.mass, "register_api_command", MagicMock())
    reloaded = ConfigController(admission.mass)
    await reloaded.setup()
    assert reloaded.get("core/discovery") == expected["core"]["discovery"]
    assert (await admission.private_trial_admission_mutate(request("clear")))[
        "persistence"
    ] == "completed"
    reopened = ConfigController(admission.mass)
    await reopened.setup()
    assert reopened.get("core/discovery/values")[CONF_PRIVATE_TRIAL_TARGET] == ""


async def test_real_parser_and_scoped_registry(
    admission: AirPlayProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The request remains uncoerced and the registered route requires management scope."""
    registry = MusicAssistant.__new__(MusicAssistant)
    registry.command_handlers = {}
    monkeypatch.setattr(admission.mass, "register_api_command", registry.register_api_command)
    admission._register_private_capture_api()
    handler = registry.command_handlers["airplay/private_trial_admission/mutate"]
    assert handler.required_scope == Scope.SYSTEM_MANAGE
    invalid = request() | {"extra": 1}
    args = parse_arguments(handler.signature, handler.type_hints, {"request": invalid})
    assert args == {"request": invalid}
    result = await cast("Awaitable[dict[str, Any]]", handler.target(**args))
    assert result["reason"] == "invalid_request"


@pytest.mark.parametrize(("allowed", "status"), [(False, 403), (True, 200)])
async def test_web_route_authorization(
    admission: AirPlayProvider,
    monkeypatch: pytest.MonkeyPatch,
    allowed: bool,
    status: int,
) -> None:
    """The JSON-RPC route enforces management scope before invoking the mutation."""
    command = "airplay/private_trial_admission/mutate"
    webserver = WebserverController(admission.mass)
    monkeypatch.setattr(webserver, "auth", MagicMock(has_users=True))
    monkeypatch.setattr(admission.mass, "metadata", MagicMock(), raising=False)
    monkeypatch.setattr(
        admission.mass, "translations", MagicMock(ensure_locale_loaded=AsyncMock()), raising=False
    )
    webserver.mass.command_handlers[command] = APICommandHandler.parse(
        command, admission.private_trial_admission_mutate, required_scope=Scope.SYSTEM_MANAGE
    )
    monkeypatch.setattr(
        webserver_module, "get_authenticated_user", AsyncMock(return_value=MagicMock())
    )
    monkeypatch.setattr(
        webserver_module, "has_scope", lambda _user, scope: allowed and scope == Scope.SYSTEM_MANAGE
    )
    payload = StreamReader(MagicMock(), 2**16, loop=asyncio.get_running_loop())
    payload.feed_data(
        json_dumps({"message_id": "1", "command": command, "args": {"request": request()}}).encode()
    )
    payload.feed_eof()
    http_request = make_mocked_request("POST", "/api", payload=payload, app=web.Application())
    response = await webserver._handle_jsonrpc_api_command(http_request)
    assert response.status == status
    assert admission.mass.config._data["core"]["discovery"]["values"][
        CONF_PRIVATE_TRIAL_TARGET
    ] == (TARGET if allowed else "")


async def test_cancel_before_entry_does_not_mutate(admission: AirPlayProvider) -> None:
    """A task cancelled before its first turn cannot have touched the target."""
    before = deepcopy(admission.mass.config._data)
    task = asyncio.create_task(admission.private_trial_admission_mutate(request()))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert admission.mass.config._data == before
