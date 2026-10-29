"""Main-only target admission on the AirPlay provider, NEVER upstream."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.helpers.private_trial import parse_trial_target
from music_assistant.providers.airplay import pairing as pairing_module
from music_assistant.providers.airplay import provider as provider_module
from music_assistant.providers.airplay import trial_binary
from music_assistant.providers.airplay.constants import COMPANION_DISCOVERY_TYPE
from music_assistant.providers.airplay.player import AirPlayPlayer
from music_assistant.providers.airplay.provider import AirPlayProvider
from tests.providers.airplay.test_player import _stub_raw_config, _stub_volume_scaling
from tests.providers.airplay.test_private_capture_api import _child, _playing_provider

TARGET = "aabbccddeeff@Main Pod"
TARGET_PLAYER_ID = "apaabbccddeeff"
OFFICE_PLAYER_ID = "ap112233445566"


@pytest.fixture(autouse=True)
def _ordinary_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the ordinary profile so results do not depend on the host; trial tests opt in."""
    monkeypatch.setattr(provider_module, "is_trial_target", lambda: False)
    monkeypatch.setattr(trial_binary, "is_trial_target", lambda: False)


def _provider(
    monkeypatch: pytest.MonkeyPatch, target: object, private: bool = True
) -> AirPlayProvider:
    """Build a bare provider whose discovery controller carries the given private target."""
    monkeypatch.setattr(provider_module, "is_trial_target", lambda: private)
    prov = AirPlayProvider.__new__(AirPlayProvider)
    prov.mass = MagicMock()
    prov.logger = logging.getLogger("test.airplay.private_target")
    monkeypatch.setattr(prov.mass.discovery, "trial_target", lambda: parse_trial_target(target))
    prov.mass.config.get_raw_player_config_value.return_value = True
    prov._is_own_airplay_receiver = AsyncMock(return_value=False)  # type: ignore[method-assign]
    return prov


def _info() -> MagicMock:
    """Build a minimal RAOP discovery record."""
    info = MagicMock()
    info.type = "_raop._tcp.local."
    info.parsed_addresses.return_value = ["192.168.1.10"]
    info.port = 7000
    info.decoded_properties = {}
    return info


@pytest.mark.parametrize("player_id", [OFFICE_PLAYER_ID, "apAABBCCDDEEFF"])
async def test_setup_rejects_off_target_player_before_any_lookup(
    monkeypatch: pytest.MonkeyPatch, player_id: str
) -> None:
    """An off-target device is dropped before config, receiver, service or probe work."""
    prov = _provider(monkeypatch, TARGET)
    await prov._setup_player(player_id, "Main Pod", _info())
    cast("MagicMock", prov.mass).config.get_raw_player_config_value.assert_not_called()
    prov._is_own_airplay_receiver.assert_not_awaited()  # type: ignore[attr-defined]
    cast("MagicMock", prov.mass).discovery.async_find_mdns_service.assert_not_called()
    cast("MagicMock", prov.mass).players.register.assert_not_called()
    cast("MagicMock", prov.mass).create_task.assert_not_called()


@pytest.mark.parametrize("target", ["", None, "Main Pod"])
async def test_setup_fails_closed_without_a_target(
    monkeypatch: pytest.MonkeyPatch, target: object
) -> None:
    """No valid target means not even the target's own id is set up."""
    prov = _provider(monkeypatch, target)
    await prov._setup_player(TARGET_PLAYER_ID, "Main Pod", _info())
    cast("MagicMock", prov.mass).discovery.async_find_mdns_service.assert_not_called()
    cast("MagicMock", prov.mass).players.register.assert_not_called()


async def test_setup_without_discovery_target_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A provider that outruns discovery setup (no target yet) must not register anything."""
    prov = _provider(monkeypatch, TARGET)
    monkeypatch.setattr(prov.mass.discovery, "trial_target", lambda: None)
    await prov._setup_player(TARGET_PLAYER_ID, "Main Pod", _info())
    cast("MagicMock", prov.mass).players.register.assert_not_called()


async def test_setup_admits_the_target_into_the_ordinary_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The target itself continues into the unchanged setup flow."""
    prov = _provider(monkeypatch, TARGET)
    # stop at the first ordinary seam: the receiver filter is reached, nothing before it skipped
    prov._is_own_airplay_receiver.return_value = True  # type: ignore[attr-defined]
    await prov._setup_player(TARGET_PLAYER_ID, "Main Pod", _info())
    prov._is_own_airplay_receiver.assert_awaited_once()  # type: ignore[attr-defined]
    cast("MagicMock", prov.mass).discovery.async_find_mdns_service.assert_not_called()


async def test_ordinary_profile_admits_every_player(monkeypatch: pytest.MonkeyPatch) -> None:
    """Outside the trial profile the admission seam is a no-op."""
    prov = _provider(monkeypatch, "", private=False)
    assert prov._admits_private_player(OFFICE_PLAYER_ID) is True
    prov._is_own_airplay_receiver.return_value = True  # type: ignore[attr-defined]
    await prov._setup_player(OFFICE_PLAYER_ID, "Office Pod", _info())
    prov._is_own_airplay_receiver.assert_awaited_once()  # type: ignore[attr-defined]


async def test_audio_format_probe_never_leaves_for_off_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The direct /info probe is the last off-mDNS seam and is gated the same way."""
    prov = _provider(monkeypatch, TARGET)
    probe = AsyncMock(return_value=None)
    monkeypatch.setattr(provider_module, "probe_audio_formats", probe)
    await prov._learn_audio_formats(MagicMock(player_id=OFFICE_PLAYER_ID), "192.168.1.20", 7000)
    probe.assert_not_awaited()
    await prov._learn_audio_formats(MagicMock(player_id=TARGET_PLAYER_ID), "192.168.1.10", 7000)
    probe.assert_awaited_once_with(prov.mass, "192.168.1.10", 7000)


async def test_cached_control_lookup_consults_discovery_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Address-based control lookups cannot query records discovery would reject."""
    prov = _provider(monkeypatch, TARGET)
    prov.mass.discovery.aiozc.zeroconf.cache.cache = {
        "office pod._companion-link._tcp.local.": {},
        "_companion-link._tcp.local.": {},
    }
    asked: list[tuple[str, str]] = []

    def _admits(service_type: str, name: str) -> bool:
        asked.append((service_type, name))
        return name.startswith("main pod")

    monkeypatch.setattr(prov.mass.discovery, "admits_mdns_record", _admits)
    info = MagicMock()
    info.async_request = AsyncMock(return_value=True)
    info.parsed_addresses.return_value = ["192.168.1.10"]
    with patch.object(provider_module, "AsyncServiceInfo", return_value=info) as service_info:
        result = await prov._find_cached_discovery_info(COMPANION_DISCOVERY_TYPE, {"192.168.1.10"})
    # the office record shares the address, so only admission can keep it unqueried
    assert result is None
    service_info.assert_not_called()
    assert asked == [(COMPANION_DISCOVERY_TYPE, "office pod._companion-link._tcp.local.")]

    prov.mass.discovery.aiozc.zeroconf.cache.cache["main pod._companion-link._tcp.local."] = {}
    with patch.object(provider_module, "AsyncServiceInfo", return_value=info) as service_info:
        result = await prov._find_cached_discovery_info(COMPANION_DISCOVERY_TYPE, {"192.168.1.10"})
    assert result is info
    service_info.assert_called_once_with(
        COMPANION_DISCOVERY_TYPE, "main pod._companion-link._tcp.local."
    )


async def test_private_ptp_daemon_requests_strict_peers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The private profile asks its owned PTP daemon to drop unregistered peers."""
    prov = AirPlayProvider.__new__(AirPlayProvider)
    prov.logger = logging.getLogger("test.airplay.private_target")
    prov.logger.setLevel(logging.INFO)
    prov._ptp_daemon = None
    prov._ptp_daemon_ready = None
    prov.dacp_id = "AABBCCDD11223344"
    prov.config = MagicMock()
    prov.mass = MagicMock()
    prov.mass.streams.get_source_ip = AsyncMock(return_value=None)

    def _consume_task(coro: Coroutine[Any, Any, None]) -> MagicMock:
        if asyncio.iscoroutine(coro):
            coro.close()
        return MagicMock()

    prov.mass.create_task.side_effect = _consume_task
    daemon = MagicMock()
    daemon.start = AsyncMock()
    factory = AsyncMock(return_value=daemon)
    monkeypatch.setattr(trial_binary, "is_trial_target", lambda: True)
    monkeypatch.setattr(trial_binary, "create_private_process", factory)
    with patch.object(provider_module, "get_cli_binary", AsyncMock(return_value="/bin/cliairplay")):
        await prov._start_ptp_daemon()
    assert factory.call_args.kwargs["env"] == {"CLIAIRPLAY_PTP_STRICT_PEERS": "1"}
    assert factory.call_args.args[0][:2] == ["/bin/cliairplay", "--ptp-daemon"]


async def test_ordinary_ptp_daemon_env_is_unchanged() -> None:
    """Outside the trial profile no private environment is added to the daemon."""
    prov = AirPlayProvider.__new__(AirPlayProvider)
    prov.logger = logging.getLogger("test.airplay.private_target")
    prov.logger.setLevel(logging.INFO)
    prov._ptp_daemon = None
    prov._ptp_daemon_ready = None
    prov.dacp_id = "AABBCCDD11223344"
    prov.config = MagicMock()
    prov.mass = MagicMock()
    prov.mass.streams.get_source_ip = AsyncMock(return_value=None)
    prov.mass.create_task.side_effect = lambda coro, **_kwargs: (coro.close(), MagicMock())[1]
    with (
        patch.object(provider_module, "get_cli_binary", AsyncMock(return_value="/bin/cliairplay")),
        patch.object(provider_module, "AsyncProcess") as process_cls,
    ):
        process_cls.return_value.start = AsyncMock(return_value=None)
        await prov._start_ptp_daemon()
    assert "env" not in process_cls.call_args.kwargs


async def test_private_stream_child_requests_strict_peers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every owned stream child carries the strict-peer request next to its capture flags."""
    provider, _player, stream = _playing_provider(monkeypatch)
    provider.config = MagicMock()
    provider.config.get_value.side_effect = lambda _key, default=None: default
    child = _child()
    monkeypatch.setattr(child, "start", AsyncMock())
    factory = AsyncMock(return_value=child)
    monkeypatch.setattr(trial_binary, "is_trial_target", lambda: True)
    monkeypatch.setattr(trial_binary, "create_private_process", factory)
    monkeypatch.setattr(stream, "_build_cli_args", AsyncMock(return_value=["unused"]))
    monkeypatch.setattr(stream, "_check_password_preflight", lambda: None)
    monkeypatch.setattr(stream.commands_pipe, "create", AsyncMock())

    def discard_reader(coro: Coroutine[Any, Any, None]) -> MagicMock:
        coro.close()
        return MagicMock()

    assert isinstance(provider.mass, MagicMock)
    provider.mass.create_task.side_effect = discard_reader
    await stream.connect()
    env = factory.call_args.kwargs["env"]
    assert env["CLIAIRPLAY_PTP_STRICT_PEERS"] == "1"
    assert env["CLIAIRPLAY_PRIVATE_NATIVE_VOLUME_CAPTURE"] == "0"


async def test_private_pairing_child_requests_strict_peers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pair-setup child is an owned private child too and carries the same request."""
    child = _child()
    monkeypatch.setattr(child, "start", AsyncMock())
    monkeypatch.setattr(child, "attach_stderr_reader", MagicMock())
    factory = AsyncMock(return_value=child)
    monkeypatch.setattr(trial_binary, "is_trial_target", lambda: True)
    monkeypatch.setattr(trial_binary, "create_private_process", factory)
    monkeypatch.setattr(pairing_module, "get_cli_binary", AsyncMock(return_value="/bin/cliairplay"))
    session = pairing_module.AirPlayPairing.__new__(pairing_module.AirPlayPairing)
    session._pair_proc = None
    session._cli_binary = "/bin/cliairplay"
    session.device_id = "AABBCCDD11223344"
    session.port = 7000
    session.address = "192.168.1.10"
    session.logger = logging.getLogger("test.airplay.private_target")
    await session._start_pair_setup_process()
    assert factory.call_args.kwargs["env"] == {"CLIAIRPLAY_PTP_STRICT_PEERS": "1"}


@pytest.mark.parametrize(
    ("private", "line", "ready"),
    [
        (True, "[PTP] daemon up: engine on 319/320, control on 127.0.0.1:9010", False),
        (True, "[PTP] daemon up: engine on 319/320, strict-peers=off", False),
        (True, "[PTP] daemon up: engine on 319/320, strict-peers=on", True),
        (False, "[PTP] daemon up: engine on 319/320, control on 127.0.0.1:9010", True),
        (False, "[PTP] daemon up: engine on 319/320, strict-peers=off", True),
    ],
)
def test_private_ptp_readiness_requires_strict_peer_attestation(
    monkeypatch: pytest.MonkeyPatch, private: bool, line: str, ready: bool
) -> None:
    """On the trial target the daemon is ready only once it attests strict peers."""
    monkeypatch.setattr(provider_module, "is_trial_target", lambda: private)
    prov = AirPlayProvider.__new__(AirPlayProvider)
    prov.logger = logging.getLogger("test.airplay.private_target")
    prov._ptp_daemon_ready = asyncio.Event()
    prov._ptp_daemon_warn_window_start = None
    prov._ptp_daemon_warns_in_window = 0
    prov._ptp_daemon_warns_suppressed = 0
    prov._handle_ptp_daemon_line(line)
    assert prov._ptp_daemon_ready.is_set() is ready


def _apple_player(
    monkeypatch: pytest.MonkeyPatch, *, private: bool, admission: bool | None, volume: bool = False
) -> AirPlayPlayer:
    """Build an Apple-manufactured player with a stream in the given admission state."""
    monkeypatch.setattr(trial_binary, "is_trial_target", lambda: private)
    provider = MagicMock()
    _stub_raw_config(provider)
    _stub_volume_scaling(provider)
    player = AirPlayPlayer(
        provider=provider,
        player_id=TARGET_PLAYER_ID,
        display_name="main-pod",
        address="192.168.40.99",
        manufacturer="Apple",
        model="HomePod",
        raop_discovery_info=None,
        airplay_discovery_info=None,
    )
    player.config.get_value.return_value = False  # type: ignore[attr-defined]
    player.stream = MagicMock(homepod_remote_admission=admission, session_volume_requested=volume)
    return player


@pytest.mark.parametrize(
    ("private", "admission", "volume", "enabled"),
    [
        (True, True, False, True),
        (True, True, True, False),
        (True, False, False, False),
        (True, None, False, False),
        (False, True, False, False),
    ],
)
def test_native_rail_follows_the_admitted_stream(
    monkeypatch: pytest.MonkeyPatch,
    private: bool,
    admission: bool | None,
    volume: bool,
    enabled: bool,
) -> None:
    """The rail is on only for a trial stream the CLI admitted, never with session volume."""
    player = _apple_player(monkeypatch, private=private, admission=admission, volume=volume)
    assert player.native_volume_rail_enabled is enabled
    player.stream = None
    assert player.native_volume_rail_enabled is False
    # the Apple echo rule itself is untouched
    assert player.ignore_volume_reports is True


def test_adopt_native_volume_never_writes_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rail report sets the level and mute flag locally; nothing is sent to the device."""
    player = _apple_player(monkeypatch, private=True, admission=True)
    player._attr_volume_level = 25
    with patch.object(AirPlayPlayer, "update_state") as update_state:
        player.adopt_native_volume(-18.0)  # one 1.5 dB step above -19.5
        levels = [player._attr_volume_level]
        muted = [player.volume_muted]
        player.adopt_native_volume(-144.0)
        levels.append(player._attr_volume_level)
        muted.append(player.volume_muted)
        player.adopt_native_volume(-22.5)
        levels.append(player._attr_volume_level)
        muted.append(player.volume_muted)
    assert levels == [40, 40, 25]
    assert muted == [False, True, False]
    assert update_state.call_count == 3
    cast("MagicMock", player.mass).create_task.assert_not_called()
    cast("MagicMock", player.stream).send_cli_command.assert_not_called()
    cast("MagicMock", player.mass).config.set_raw_player_config_value.assert_called_with(
        TARGET_PLAYER_ID, "stored_volume", 25
    )


def test_adopt_native_volume_respects_echo_window_and_ignore_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Our own level echoing back, or the explicit ignore option, still wins on the rail."""
    player = _apple_player(monkeypatch, private=True, admission=True)
    player._attr_volume_level = 25
    player.suppress_volume_reports(10)
    with patch.object(AirPlayPlayer, "update_state") as update_state:
        player.adopt_native_volume(-18.0)
        player._volume_reports_ignored_until = 0
        player.config.get_value.return_value = True  # type: ignore[attr-defined]
        player.adopt_native_volume(-18.0)
    update_state.assert_not_called()
    assert player._attr_volume_level == 25


@pytest.mark.parametrize(
    ("private", "remote", "volume", "rail"),
    [
        (True, True, False, "1"),
        (True, True, True, "0"),
        (True, False, False, "0"),
        (False, True, False, "0"),
    ],
)
async def test_stream_child_requests_the_native_rail(
    monkeypatch: pytest.MonkeyPatch, private: bool, remote: bool, volume: bool, rail: str
) -> None:
    """The rail env rides HomePod-remote admission on the trial, never with session volume."""
    provider, _player, stream = _playing_provider(monkeypatch)
    values = {"experimental_homepod_remote": remote, "experimental_session_volume": volume}
    provider.config = MagicMock()
    provider.config.get_value.side_effect = lambda key, default=None: values.get(key, default)
    child = _child()
    monkeypatch.setattr(child, "start", AsyncMock())
    factory = AsyncMock(return_value=child)
    monkeypatch.setattr(trial_binary, "is_trial_target", lambda: private)
    monkeypatch.setattr(trial_binary, "create_private_process", factory)
    monkeypatch.setattr(stream, "_build_cli_args", AsyncMock(return_value=["unused"]))
    monkeypatch.setattr(stream, "_check_password_preflight", lambda: None)
    monkeypatch.setattr(stream.commands_pipe, "create", AsyncMock())
    captured: dict[str, str] = {}

    def discard_reader(coro: Coroutine[Any, Any, None]) -> MagicMock:
        coro.close()
        return MagicMock()

    assert isinstance(provider.mass, MagicMock)
    provider.mass.create_task.side_effect = discard_reader
    if not private:
        monkeypatch.setattr(
            "music_assistant.providers.airplay.stream.AsyncProcess",
            lambda *_args, **kwargs: captured.update(kwargs.get("env", {})) or child,
        )
    await stream.connect()
    env = factory.call_args.kwargs["env"] if private else captured
    assert env["CLIAIRPLAY_NATIVE_VOLUME_RAIL"] == rail
