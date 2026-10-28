"""Main-only discovery admission for the private trial profile, NEVER upstream."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from zeroconf import ServiceStateChange

from music_assistant.controllers.discovery import controller as discovery_module
from music_assistant.helpers.private_trial import CONF_PRIVATE_TRIAL_TARGET
from music_assistant.mass import MusicAssistant

TARGET = "aabbccddeeff@Main Pod"
TARGET_RAOP = "aabbccddeeff@main pod._raop._tcp.local."
TARGET_AIRPLAY = "main pod._airplay._tcp.local."
OFFICE_RAOP = "112233445566@office pod._raop._tcp.local."
OFFICE_AIRPLAY = "office pod._airplay._tcp.local."
OFFICE_COMPANION = "office pod._companion-link._tcp.local."


class StubAirPlayProvider:
    """Minimal mDNS-subscribed provider stub."""

    def __init__(self) -> None:
        """Initialize the provider stub."""
        self.instance_id = "stub_airplay"
        self.name = "Stub AirPlay"
        self.available = True
        self.logger = MagicMock()
        self.manifest = MagicMock(
            mdns_discovery=["_raop._tcp.local.", "_airplay._tcp.local."], upnp_discovery=None
        )
        self.on_mdns_service_state_change = AsyncMock()


def _configure_target(mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch, value: object) -> None:
    """Pretend to be the trial target with the given private target value."""
    monkeypatch.setattr(discovery_module, "is_trial_target", lambda: True)
    config = MagicMock()
    config.get_value = lambda key, default=None: (
        value if key == CONF_PRIVATE_TRIAL_TARGET else default
    )
    monkeypatch.setattr(mass.discovery, "config", config)


@pytest.fixture
def provider(mass: MusicAssistant) -> Any:
    """Register a stub provider for the test and remove it afterwards."""
    stub = StubAirPlayProvider()
    mass._providers[stub.instance_id] = stub  # type: ignore[assignment]
    yield stub
    mass._providers.pop(stub.instance_id, None)
    mass.discovery.on_provider_unload(stub.instance_id)


async def _drain() -> None:
    """Let the state-change tasks the controller spawned run."""
    for _ in range(3):
        await asyncio.sleep(0)


async def test_ordinary_profile_admits_every_record(mass: MusicAssistant) -> None:
    """Outside the trial profile nothing changes: every record is admitted."""
    assert mass.discovery.admits_mdns_record("_raop._tcp.local.", OFFICE_RAOP) is True
    assert mass.discovery.admits_mdns_record("_raop._tcp.local.", TARGET_RAOP) is True


@pytest.mark.parametrize("value", ["", None, "Main Pod", "zz@Main Pod"])
async def test_trial_profile_fails_closed_without_a_target(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch, provider: Any, value: object
) -> None:
    """With no valid target configured the trial profile resolves nothing at all."""
    _configure_target(mass, monkeypatch, value)
    mass.discovery.aiozc.zeroconf.cache.cache = {TARGET_RAOP: {}, OFFICE_RAOP: {}}
    with patch.object(discovery_module, "AsyncServiceInfo") as service_info:
        mass.discovery._on_mdns_service_state_change(
            mass.discovery.aiozc.zeroconf,
            "_raop._tcp.local.",
            TARGET_RAOP,
            ServiceStateChange.Added,
        )
        await _drain()
        await mass.discovery._replay_mdns_discovery(provider)
        found = await mass.discovery.async_find_mdns_service("_raop._tcp.local.", timeout=0.05)

    service_info.assert_not_called()
    provider.on_mdns_service_state_change.assert_not_awaited()
    assert found is None


async def test_trial_profile_never_queries_before_setup(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Admission before the controller has its config fails closed too."""
    monkeypatch.setattr(discovery_module, "is_trial_target", lambda: True)
    monkeypatch.delattr(mass.discovery, "config", raising=False)
    assert mass.discovery.trial_target() is None
    assert mass.discovery.admits_mdns_record("_raop._tcp.local.", TARGET_RAOP) is False


async def test_state_change_resolves_only_the_target(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch, provider: Any
) -> None:
    """A browser event for any other device never turns into a directed query or callback."""
    _configure_target(mass, monkeypatch, TARGET)
    info = MagicMock()
    info.async_request = AsyncMock(return_value=True)
    with patch.object(discovery_module, "AsyncServiceInfo", return_value=info) as service_info:
        for service_type, name in (
            ("_raop._tcp.local.", OFFICE_RAOP),
            ("_airplay._tcp.local.", OFFICE_AIRPLAY),
            ("_companion-link._tcp.local.", OFFICE_COMPANION),
            ("_airplay._tcp.local.", "main pod.office._airplay._tcp.local."),
            ("_raop._tcp.local.", TARGET_AIRPLAY),
        ):
            mass.discovery._on_mdns_service_state_change(
                mass.discovery.aiozc.zeroconf, service_type, name, ServiceStateChange.Added
            )
        await _drain()
        service_info.assert_not_called()
        provider.on_mdns_service_state_change.assert_not_awaited()

        mass.discovery._on_mdns_service_state_change(
            mass.discovery.aiozc.zeroconf,
            "_raop._tcp.local.",
            TARGET_RAOP,
            ServiceStateChange.Added,
        )
        await _drain()

    service_info.assert_called_once_with("_raop._tcp.local.", TARGET_RAOP)
    info.async_request.assert_awaited_once()
    provider.on_mdns_service_state_change.assert_awaited_once_with(
        TARGET_RAOP, ServiceStateChange.Added, info
    )


async def test_removed_off_target_record_is_not_dispatched(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch, provider: Any
) -> None:
    """Even query-free Removed events stay away from providers for off-target devices."""
    _configure_target(mass, monkeypatch, TARGET)
    mass.discovery._on_mdns_service_state_change(
        mass.discovery.aiozc.zeroconf, "_raop._tcp.local.", OFFICE_RAOP, ServiceStateChange.Removed
    )
    await _drain()
    provider.on_mdns_service_state_change.assert_not_awaited()


async def test_replay_skips_cached_off_target_records(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch, provider: Any
) -> None:
    """Replaying the shared cache after a provider load queries only the target."""
    _configure_target(mass, monkeypatch, TARGET)
    mass.discovery.aiozc.zeroconf.cache.cache = {
        OFFICE_RAOP: {},
        TARGET_RAOP: {},
        OFFICE_AIRPLAY: {},
        TARGET_AIRPLAY: {},
    }
    requested: list[str] = []

    class _Info:
        def __init__(self, _service_type: str, name: str) -> None:
            self.name = name

        async def async_request(self, _zeroconf: object, _timeout: float) -> bool:
            requested.append(self.name)
            return True

    with patch.object(discovery_module, "AsyncServiceInfo", new=_Info):
        await mass.discovery._replay_mdns_discovery(provider)

    assert sorted(requested) == sorted([TARGET_RAOP, TARGET_AIRPLAY])
    assert sorted(
        call.args[0] for call in provider.on_mdns_service_state_change.await_args_list
    ) == sorted([TARGET_RAOP, TARGET_AIRPLAY])


async def test_find_service_skips_off_target_candidates(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unfiltered related-service lookups cannot probe a cached off-target record."""
    _configure_target(mass, monkeypatch, TARGET)
    mass.discovery.aiozc.zeroconf.cache.cache = {OFFICE_COMPANION: {}}
    with patch.object(discovery_module, "AsyncServiceInfo") as service_info:
        found = await mass.discovery.async_find_mdns_service(
            "_companion-link._tcp.local.", timeout=0.05
        )
    assert found is None
    service_info.assert_not_called()

    mass.discovery.aiozc.zeroconf.cache.cache = {OFFICE_AIRPLAY: {}, TARGET_AIRPLAY: {}}
    info = MagicMock()
    info.async_request = AsyncMock(return_value=True)
    with patch.object(discovery_module, "AsyncServiceInfo", return_value=info) as service_info:
        found = await mass.discovery.async_find_mdns_service("_airplay._tcp.local.", timeout=0.5)
    assert found is info
    service_info.assert_called_once_with("_airplay._tcp.local.", TARGET_AIRPLAY)


async def test_trial_profile_browses_only_loadable_provider_types(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The global browser never subscribes to types of providers the profile cannot load."""
    monkeypatch.setattr(discovery_module, "is_trial_target", lambda: True)
    monkeypatch.setattr(
        discovery_module, "should_load_provider", lambda domain, _builtin: domain == "airplay"
    )
    manifests = [
        MagicMock(domain="airplay", builtin=False, mdns_discovery=["_raop._tcp.local."]),
        MagicMock(domain="sonos", builtin=False, mdns_discovery=["_sonos._tcp.local."]),
        MagicMock(domain="sendspin", builtin=True, mdns_discovery=["_sendspin._tcp.local."]),
        MagicMock(domain="builtin", builtin=True, mdns_discovery=None),
    ]
    monkeypatch.setattr(mass, "get_provider_manifests", lambda: manifests)
    with patch.object(discovery_module, "AsyncServiceBrowser") as browser:
        await mass.discovery._setup_mdns_browser()
    assert browser.call_args.args[1] == ["_raop._tcp.local."]


async def test_private_target_entry_only_exists_on_the_trial_target(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordinary profiles never grow the private config entry."""
    monkeypatch.setattr(discovery_module, "is_trial_target", lambda: False)
    keys = {entry.key for entry in await mass.discovery.get_config_entries()}
    assert CONF_PRIVATE_TRIAL_TARGET not in keys
    monkeypatch.setattr(discovery_module, "is_trial_target", lambda: True)
    entries = {entry.key: entry for entry in await mass.discovery.get_config_entries()}
    entry = entries[CONF_PRIVATE_TRIAL_TARGET]
    assert entry.default_value == ""
    assert entry.advanced is True
    assert entry.requires_reload is False
