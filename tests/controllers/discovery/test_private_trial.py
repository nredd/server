"""Private trial publication suppression without changing provider discovery."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from zeroconf import ServiceStateChange

from music_assistant.controllers.discovery import controller as discovery_module
from music_assistant.controllers.discovery.controller import HA_ANNOUNCE_TIMER_ID
from music_assistant.helpers.private_trial import CONF_PRIVATE_TRIAL_TARGET
from music_assistant.mass import MusicAssistant


@pytest.mark.parametrize("private", [True, False])
async def test_cold_setup_and_periodic_publication(
    mass_minimal: MusicAssistant, monkeypatch: pytest.MonkeyPatch, private: bool
) -> None:
    """Cold start, state refresh and daily callbacks guard publication before token creation."""
    mass = mass_minimal
    discovery = mass.discovery
    mass.running_as_hass_addon = True
    mass.webserver = MagicMock(
        publish_addresses=["127.0.0.1"],
        publish_port=8095,
        auth=MagicMock(get_homeassistant_system_user_token=AsyncMock(return_value="private-token")),
    )
    discovery._aiozc = MagicMock(
        async_register_service=AsyncMock(),
        async_update_service=AsyncMock(),
    )
    cfg = await mass.config.get_core_config("discovery")
    monkeypatch.setenv("HOSTNAME", "test-addon")
    with (
        patch(
            "music_assistant.controllers.discovery.controller.is_trial_target", return_value=private
        ),
        patch(
            "music_assistant.controllers.discovery.controller.supervisor_request",
            new_callable=AsyncMock,
        ) as supervisor,
        patch.object(discovery, "_setup_mdns_browser", new_callable=AsyncMock) as browser,
        patch.object(mass, "call_later") as timer,
        patch.object(mass, "get_server_info", return_value=MagicMock(to_dict=dict)),
    ):
        await discovery.setup(cfg)
        browser.assert_awaited_once()
        await discovery._on_core_state_updated(MagicMock())
        # Exercise the actual periodic callback for normal mode; a trial must not
        # schedule it, but even a stale callback's publication method is guarded.
        if not private:
            callbacks = [
                call.args[1]
                for call in timer.call_args_list
                if call.kwargs.get("task_id") == HA_ANNOUNCE_TIMER_ID
            ]
            assert len(callbacks) == 1
            tasks: list[Coroutine[Any, Any, None]] = []
            with patch.object(mass, "create_task", side_effect=tasks.append):
                callbacks[0]()
            for task in tasks:
                await task
        await discovery._announce_to_homeassistant()
        token = mass.webserver.auth.get_homeassistant_system_user_token
        if private:
            token.assert_not_awaited()
            supervisor.assert_not_awaited()
            discovery._aiozc.async_register_service.assert_not_awaited()
            discovery._aiozc.async_update_service.assert_not_awaited()
            assert not any(
                call.kwargs.get("task_id") == HA_ANNOUNCE_TIMER_ID for call in timer.call_args_list
            )
        else:
            assert token.await_count == 3
            assert supervisor.await_count == 3
            discovery._aiozc.async_register_service.assert_awaited_once()
            discovery._aiozc.async_update_service.assert_awaited_once()
    discovery._aiozc = None


async def test_trial_setup_clears_stale_admission_before_browser(
    mass_minimal: MusicAssistant,
) -> None:
    """Private startup clears persisted admission before any browser callback can resolve it."""
    mass = mass_minimal
    discovery = mass.discovery
    discovery._aiozc = MagicMock()
    mass.config.set_raw_core_config_value(
        "discovery", CONF_PRIVATE_TRIAL_TARGET, "aabbccddeeff@test"
    )
    await mass.config.async_save()
    config = await mass.config.get_core_config("discovery")
    provider = MagicMock(
        instance_id="private-airplay",
        available=True,
        manifest=MagicMock(mdns_discovery=["_raop._tcp.local."], upnp_discovery=None),
        on_mdns_service_state_change=AsyncMock(),
    )
    mass._providers[provider.instance_id] = provider

    async def setup_browser() -> None:
        assert discovery.trial_target() is None
        discovery._on_mdns_service_state_change(
            MagicMock(),
            "_raop._tcp.local.",
            "aabbccddeeff@test._raop._tcp.local.",
            ServiceStateChange.Added,
        )
        for _ in range(3):
            await asyncio.sleep(0)

    with (
        patch(
            "music_assistant.controllers.discovery.controller.is_trial_target", return_value=True
        ),
        patch.object(discovery, "_setup_mdns_browser", side_effect=setup_browser) as browser,
        patch.object(discovery_module, "AsyncServiceInfo") as service_info,
    ):
        await discovery.setup(config)

    browser.assert_awaited_once()
    service_info.assert_not_called()
    provider.on_mdns_service_state_change.assert_not_awaited()
    assert mass.config.get_raw_core_config_value("discovery", CONF_PRIVATE_TRIAL_TARGET) == ""
    mass._providers.pop(provider.instance_id)
    discovery._aiozc = None


async def test_private_target_can_be_admitted_after_boot(
    mass_minimal: MusicAssistant,
) -> None:
    """The existing config API admits an explicit target after private startup resets it."""
    mass = mass_minimal
    discovery = mass.discovery
    discovery._aiozc = MagicMock()
    provider = MagicMock(
        instance_id="private-airplay",
        available=True,
        manifest=MagicMock(mdns_discovery=["_raop._tcp.local."], upnp_discovery=None),
        on_mdns_service_state_change=AsyncMock(),
    )
    mass._providers[provider.instance_id] = provider

    with (
        patch(
            "music_assistant.controllers.discovery.controller.is_trial_target", return_value=True
        ),
        patch.object(discovery, "_setup_mdns_browser", new_callable=AsyncMock),
    ):
        await discovery.setup(await mass.config.get_core_config("discovery"))
        await mass.config.save_core_config(
            "discovery", {CONF_PRIVATE_TRIAL_TARGET: "aabbccddeeff@test"}
        )
        info = MagicMock()
        info.async_request = AsyncMock(return_value=True)
        with patch.object(discovery_module, "AsyncServiceInfo", return_value=info):
            discovery._on_mdns_service_state_change(
                MagicMock(),
                "_raop._tcp.local.",
                "aabbccddeeff@test._raop._tcp.local.",
                ServiceStateChange.Added,
            )
            for _ in range(3):
                await asyncio.sleep(0)

    info.async_request.assert_awaited_once()
    provider.on_mdns_service_state_change.assert_awaited_once_with(
        "aabbccddeeff@test._raop._tcp.local.", ServiceStateChange.Added, info
    )
    mass._providers.pop(provider.instance_id)
    discovery._aiozc = None


async def test_ordinary_setup_preserves_private_target_value(
    mass_minimal: MusicAssistant,
) -> None:
    """Ordinary startup leaves persisted values untouched."""
    mass = mass_minimal
    discovery = mass.discovery
    mass.config.set_raw_core_config_value("discovery", CONF_PRIVATE_TRIAL_TARGET, "retained")
    config = await mass.config.get_core_config("discovery")
    with (
        patch(
            "music_assistant.controllers.discovery.controller.is_trial_target", return_value=False
        ),
        patch.object(discovery, "_setup_mdns_browser", new_callable=AsyncMock),
        patch.object(discovery, "_register_mass_service", new_callable=AsyncMock),
    ):
        await discovery.setup(config)

    assert (
        mass.config.get_raw_core_config_value("discovery", CONF_PRIVATE_TRIAL_TARGET) == "retained"
    )


async def test_trial_provider_browser_and_callback_unchanged(mass_minimal: MusicAssistant) -> None:
    """RAOP browsing and provider callbacks still operate for the configured target."""
    mass = mass_minimal
    discovery = mass.discovery
    discovery._aiozc = MagicMock()
    provider = MagicMock(
        instance_id="private-airplay",
        available=True,
        manifest=MagicMock(domain="airplay", builtin=False, mdns_discovery=["_raop._tcp.local."]),
        on_mdns_service_state_change=AsyncMock(),
    )
    mass._providers[provider.instance_id] = provider
    tasks: list[Coroutine[Any, Any, None]] = []
    config = MagicMock()
    config.get_value = lambda key, default=None: (
        "aabbccddeeff@test" if key == CONF_PRIVATE_TRIAL_TARGET else default
    )
    with (
        patch(
            "music_assistant.controllers.discovery.controller.is_trial_target", return_value=True
        ),
        patch("music_assistant.controllers.discovery.controller.AsyncServiceBrowser") as browser,
        patch.object(mass, "get_provider_manifests", return_value=[provider.manifest]),
        patch.object(mass, "create_task", side_effect=lambda coro, **_kwargs: tasks.append(coro)),
        patch.object(discovery, "config", config, create=True),
    ):
        await discovery._setup_mdns_browser()
        assert browser.call_args.args[1] == ["_raop._tcp.local."]
        discovery._on_mdns_service_state_change(
            MagicMock(),
            "_raop._tcp.local.",
            "aabbccddeeff@test._raop._tcp.local.",
            ServiceStateChange.Removed,
        )
        for task in tasks:
            await task
        provider.on_mdns_service_state_change.assert_awaited_once_with(
            "aabbccddeeff@test._raop._tcp.local.",
            ServiceStateChange.Removed,
            None,
        )
    discovery._mdns_browser = None
    discovery._aiozc = None
    mass._providers.pop(provider.instance_id)
