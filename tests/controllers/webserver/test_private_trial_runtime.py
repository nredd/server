"""Private trial webserver runtime isolation tests."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if TYPE_CHECKING:
    from music_assistant_models.config_entries import CoreConfig

from music_assistant.controllers.webserver.controller import WebserverController
from music_assistant.helpers import private_trial


@pytest.mark.parametrize("trial", [False, True])
async def test_setup_profiles_ingress_and_actual_api_port(
    monkeypatch: pytest.MonkeyPatch, trial: bool, tmp_path: Path
) -> None:
    """Use the private direct API without touching ingress discovery in trial mode."""
    mass = MagicMock()
    mass.running_as_hass_addon = True
    mass.config.get_raw_core_config_value.return_value = "GLOBAL"
    controller = WebserverController(mass)
    controller.auth = MagicMock(setup=AsyncMock(), has_users=True)
    controller.remote_access = MagicMock(setup=AsyncMock())
    server = MagicMock()

    async def setup(**kwargs: Any) -> None:
        """Record the bind request and expose the server's actual resolved endpoint."""
        server.bind_ip = kwargs["bind_ip"]
        server.port = 18096 if trial else 8096

    server.setup = AsyncMock(side_effect=setup)
    controller._server = server
    cfg_values = {"bind_port": 8095, "bind_ip": "192.0.2.10", "enable_ssl": False}
    config = MagicMock()
    config.get_value.side_effect = lambda key, default=None: cfg_values.get(key, default)
    ingress_ips = AsyncMock(return_value=("172.30.32.2",))
    if trial:
        ingress_ips.side_effect = AssertionError("trial must not inspect ingress IPs")
    monkeypatch.setattr(private_trial, "is_trial_target", lambda: trial)

    with (
        patch("music_assistant.controllers.webserver.controller.get_ip_addresses", ingress_ips),
        patch(
            "music_assistant.controllers.webserver.controller.get_publish_ip_candidates",
            AsyncMock(return_value=("192.0.2.10",)),
        ),
        patch(
            "music_assistant.controllers.webserver.controller.locate_frontend",
            return_value=str(tmp_path),
        ),
    ):
        await controller.setup(cast("CoreConfig", config))

    assert server.setup.await_args is not None
    setup_args = server.setup.await_args.kwargs
    assert setup_args["bind_port"] == (private_trial.PRIVATE_API_PORT if trial else 8095)
    assert setup_args["ingress_tcp_site_params"] == (None if trial else ("172.30.32.2", 8094))
    if trial:
        ingress_ips.assert_not_awaited()
        assert controller.base_url == "http://192.0.2.10:18096"
    else:
        ingress_ips.assert_awaited_once_with(include_ipv6=True)
        assert controller.base_url == "http://192.0.2.10:8096"
