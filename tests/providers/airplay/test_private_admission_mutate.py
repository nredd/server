"""Tests for the private atomic admission mutation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from copy import deepcopy
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.streams import StreamReader
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.auth import Scope
from music_assistant_models.config_entries import ConfigEntry, CoreConfig, ProviderConfig
from music_assistant_models.enums import ConfigEntryType

from music_assistant.controllers.config import ConfigController
from music_assistant.controllers.webserver import controller as webserver_module
from music_assistant.controllers.webserver.controller import WebserverController
from music_assistant.helpers.api import APICommandHandler, parse_arguments
from music_assistant.helpers.json import json_dumps
from music_assistant.helpers.private_trial import CONF_PRIVATE_TRIAL_TARGET
from music_assistant.mass import MusicAssistant
from music_assistant.providers.airplay import provider as provider_module
from music_assistant.providers.airplay.constants import CONF_PRIVATE_NATIVE_VOLUME_CAPTURE
from music_assistant.providers.airplay.provider import AirPlayProvider

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
    provider.__dict__["manifest"] = SimpleNamespace(domain="airplay")
    provider.__dict__["config"] = ProviderConfig.parse(entries, raw)
    provider.__dict__["get_players"] = MagicMock(return_value=[])
    provider.__dict__["logger"] = MagicMock()
    provider.available = True
    mass_minimal._providers[INSTANCE] = provider
    target = ConfigEntry(key=CONF_PRIVATE_TRIAL_TARGET, type=ConfigEntryType.STRING, value="")
    monkeypatch.setattr(
        mass_minimal.discovery,
        "config",
        CoreConfig.parse([target], {"domain": "discovery", "values": {}}),
        raising=False,
    )
    mass_minimal.discovery.config.values[CONF_PRIVATE_TRIAL_TARGET].value = ""
    await mass_minimal.config.async_save()
    return provider


def request(mode: str = "admit") -> dict[str, str]:
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
    loop.call_soon(
        lambda: observed.append(cfg._data["core"]["discovery"]["values"][CONF_PRIVATE_TRIAL_TARGET])
    )

    async def flush() -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(cfg, "async_save", flush)
    result = await admission.private_trial_admission_mutate(request())
    assert result["status"] == "applied"
    assert observed == [TARGET]


async def test_later_stale_writer_is_not_serialized(admission: AirPlayProvider) -> None:
    """An older writer can still overwrite the new target after admission."""
    cfg = admission.mass.config
    stale = deepcopy(cfg._data["core"]["discovery"])
    assert (await admission.private_trial_admission_mutate(request()))["status"] == "applied"
    cfg.set("core/discovery", stale)
    assert cfg._data["core"]["discovery"]["values"][CONF_PRIVATE_TRIAL_TARGET] == ""
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
