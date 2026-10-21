"""Private runtime isolation regressions for the native AirPlay trial."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import ProviderType
from music_assistant_models.errors import SetupFailedError
from music_assistant_models.provider import ProviderManifest

from music_assistant.helpers import private_trial
from music_assistant.mass import MusicAssistant


def _manifest(
    domain: str, *, builtin: bool = False, mdns: list[str] | None = None
) -> ProviderManifest:
    """Create a real manifest for an isolated provider loading seam."""
    return ProviderManifest(
        type=ProviderType.PLAYER,
        domain=domain,
        name=domain,
        description=domain,
        codeowners=[],
        builtin=builtin,
        mdns_discovery=mdns,
    )


def _config(domain: str) -> ProviderConfig:
    """Create a real enabled provider config for an isolated loading seam."""
    return ProviderConfig(
        values={}, type=ProviderType.PLAYER, domain=domain, instance_id=f"{domain}--private"
    )


def _mass() -> MagicMock:
    """Provide declared dependencies for unbound calls to the real server loaders."""
    mass = MagicMock(spec=MusicAssistant)
    mass._provider_manifests = {}
    mass._providers = {}
    mass.config = MagicMock()
    mass.config.create_builtin_provider_config = AsyncMock(return_value=True)
    mass.config.get_player_configs = AsyncMock(return_value=[])
    mass.config.get_provider_configs = AsyncMock(return_value=[])
    mass.discovery = MagicMock()
    mass.discovery.aiozc.zeroconf.cache.cache = {"speaker._sonos._tcp.local."}
    mass.load_provider = AsyncMock()
    mass.unload_provider = AsyncMock()
    mass.create_task = lambda coro, **_kwargs: asyncio.create_task(coro)
    return mass


@pytest.mark.parametrize("trial", [False, True])
async def test_builtin_creation_and_saved_loads_are_profiled(
    monkeypatch: pytest.MonkeyPatch, trial: bool
) -> None:
    """Never create or load retained Sendspin configs in the trial profile."""
    mass = _mass()
    mass._provider_manifests = {
        "builtin": _manifest("builtin", builtin=True),
        "sendspin": _manifest("sendspin", builtin=True),
        "sendspin_source": _manifest("sendspin_source", builtin=True),
    }
    mass.config.get_provider_configs.return_value = [
        _config("builtin"),
        _config("sendspin"),
        _config("sendspin_source"),
    ]
    monkeypatch.setattr(private_trial, "is_trial_target", lambda: trial)

    await MusicAssistant._load_builtin_providers(mass)

    expected = {"builtin"} if trial else {"builtin", "sendspin", "sendspin_source"}
    assert {
        call.args[0] for call in mass.config.create_builtin_provider_config.await_args_list
    } == expected
    assert {call.args[0] for call in mass.load_provider.await_args_list} == {
        f"{domain}--private" for domain in expected
    }


@pytest.mark.parametrize("trial", [False, True])
async def test_default_and_saved_regular_loads_are_profiled(
    monkeypatch: pytest.MonkeyPatch, trial: bool
) -> None:
    """Load only AirPlay from fresh defaults or retained regular configs in trial mode."""
    mass = _mass()
    mass._provider_manifests = {
        "airplay": _manifest("airplay"),
        "sonos": _manifest("sonos", mdns=["_sonos._tcp.local."]),
        "saved_only": _manifest("saved_only"),
    }
    mass.config.get.return_value = set()
    mass.config.get_provider_configs.return_value = [
        _config("airplay"),
        _config("sonos"),
        _config("saved_only"),
        _config("missing_manifest"),
    ]
    monkeypatch.setattr(private_trial, "is_trial_target", lambda: trial)

    await MusicAssistant._load_providers(mass)

    expected = {"airplay"} if trial else {"airplay", "sonos", "saved_only", "missing_manifest"}
    assert {
        call.args[0] for call in mass.config.create_builtin_provider_config.await_args_list
    } == ({"airplay"} if trial else {"airplay", "sonos"})
    assert {call.args[0] for call in mass.load_provider.await_args_list} == {
        f"{domain}--private" for domain in expected
    }


async def test_manual_nontrial_provider_load_is_refused_before_unload_or_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject manual non-AirPlay loads before they can affect a private host."""
    mass = _mass()
    mass._provider_manifests = {"chromecast": _manifest("chromecast")}
    monkeypatch.setattr(private_trial, "is_trial_target", lambda: True)

    with pytest.raises(SetupFailedError, match="private trial"):
        await MusicAssistant._load_provider(mass, _config("chromecast"))

    mass.unload_provider.assert_not_awaited()
