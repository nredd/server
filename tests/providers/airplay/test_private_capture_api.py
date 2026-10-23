"""Tests for the private native-volume capture API."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Coroutine
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.auth import Scope
from music_assistant_models.enums import PlaybackState

from music_assistant.helpers.json import SerializableType
from music_assistant.helpers.process import AsyncProcess
from music_assistant.mass import MusicAssistant
from music_assistant.providers.airplay import provider as provider_module
from music_assistant.providers.airplay import trial_binary
from music_assistant.providers.airplay.constants import (
    CONF_EXPERIMENTAL_HOMEPOD_REMOTE,
    CONF_EXPERIMENTAL_SESSION_VOLUME,
    CONF_PRIVATE_NATIVE_VOLUME_CAPTURE,
    StreamingProtocol,
)
from music_assistant.providers.airplay.private_volume_capture import PrivateVolumeCapture
from music_assistant.providers.airplay.provider import AirPlayProvider
from music_assistant.providers.airplay.stream import AirPlayStream, PrivateCaptureRequest

_COMMAND = "airplay/private_native_volume_capture/get"
_READINESS_COMMAND = "airplay/private_native_volume_capture/readiness/get"


def _provider() -> tuple[AirPlayProvider, MagicMock]:
    """Build a bare provider with only private capture state."""
    provider = AirPlayProvider.__new__(AirPlayProvider)
    mass = MagicMock()
    provider.mass = mass
    provider._private_capture_history = deque(maxlen=16)
    provider._ptp_daemon = None
    provider._ptp_daemon_ready = None
    provider._trial_identity = {}
    provider._unregister_private_capture_api = None
    provider._unregister_private_capture_readiness_api = None
    return provider, mass


def _child() -> trial_binary.TrialAsyncProcess:
    """Use the real current-receipt guard with a controlled subprocess handle."""
    child = trial_binary.TrialAsyncProcess(["unused"], asset_fd=-1)
    child.proc = MagicMock(returncode=None)
    child._trusted_receipt = trial_binary._TrustedChildReceipt(
        child, child.proc, trial_binary.EXPECTED_SHA
    )
    return child


def _playing_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[AirPlayProvider, MagicMock, AirPlayStream]:
    """Build a connected generation with real stream and receipt implementations."""
    provider, _ = _provider()
    provider._ptp_daemon = _child()
    provider._ptp_daemon_ready = asyncio.Event()
    provider._ptp_daemon_ready.set()
    player = MagicMock(provider=provider, playback_state=PlaybackState.PLAYING)
    player.device_info.mac_address = "00:00:00:00:00:01"
    stream = AirPlayStream(player)
    player.stream = stream
    stream._cli_proc = _child()
    stream._connected.set()
    stream._update_elapsed(0)
    stream.private_capture_request = PrivateCaptureRequest(stream._cli_proc, True, True, False)
    stream.native_volume_capture = PrivateVolumeCapture()
    stream.native_volume_capture.trust(stream._cli_proc)
    stream._parse_homepod_remote_admission_status("[STATUS] homepod_remote_admission enabled=1")
    stream._parse_route_status("[STATUS] route protocol=airplay2 flow=native timing=ptp buffered=0")
    monkeypatch.setattr(provider_module, "is_trial_target", lambda: True)
    monkeypatch.setattr(provider, "get_players", lambda: [player])
    monkeypatch.setattr(provider_module, "installer_provenance", dict)
    return provider, player, stream


async def test_registers_fixed_scoped_route_only_for_trial_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The private route has one fixed name and system-management scope."""
    provider, mass = _provider()
    unregister_capture = MagicMock()
    unregister_readiness = MagicMock()
    mass.register_api_command.side_effect = (unregister_capture, unregister_readiness)
    monkeypatch.setattr(provider_module, "is_trial_target", lambda: True)

    provider._register_private_capture_api()

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
        provider._register_private_capture_api()

    assert provider._unregister_private_capture_api is None


async def test_second_route_collision_rolls_back_real_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A collision must retain the incumbent and release this provider's first route."""
    provider, mass = _provider()
    registry = MusicAssistant.__new__(MusicAssistant)
    registry.command_handlers = {}
    registry.register_api_command(_READINESS_COMMAND, provider.get_private_native_volume_capture)
    incumbent = registry.command_handlers[_READINESS_COMMAND]
    mass.register_api_command.side_effect = registry.register_api_command
    monkeypatch.setattr(provider_module, "is_trial_target", lambda: True)

    with pytest.raises(RuntimeError, match="already registered"):
        provider._register_private_capture_api()

    assert registry.command_handlers == {_READINESS_COMMAND: incumbent}
    assert provider._unregister_private_capture_api is None


async def test_route_collision_fails_async_initialization_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registration errors escape init, rather than core's swallowed post-load hook."""
    provider, mass = _provider()
    registry = MusicAssistant.__new__(MusicAssistant)
    registry.command_handlers = {}
    registry.register_api_command(_READINESS_COMMAND, provider.get_private_native_volume_capture)
    incumbent = registry.command_handlers[_READINESS_COMMAND]
    mass.register_api_command.side_effect = registry.register_api_command
    mass.server_id = "0" * 32
    provider.logger = MagicMock()
    monkeypatch.setattr(provider_module, "is_trial_target", lambda: True)
    monkeypatch.setattr(trial_binary, "is_trial_target", lambda: False)
    for name in (
        "_set_pyatv_log_level",
        "_drop_unverified_password_markers",
        "_reset_auto_pinned_compat_modes",
    ):
        monkeypatch.setattr(provider, name, lambda: None)
    monkeypatch.setattr(provider_module, "SendspinBridgeManager", MagicMock())
    monkeypatch.setattr(provider_module, "AirPlayDashboards", MagicMock())
    monkeypatch.setattr(provider_module, "select_free_port", AsyncMock(return_value=12345))
    monkeypatch.setattr(provider_module, "get_ip_pton", AsyncMock(return_value=b"\x7f\x00\x00\x01"))
    monkeypatch.setattr(asyncio, "start_server", AsyncMock())
    monkeypatch.setattr(provider, "_register_dacp_service", AsyncMock())
    monkeypatch.setattr(provider, "_start_ptp_daemon", AsyncMock())
    cleanup = AsyncMock()
    monkeypatch.setattr(provider, "unload", cleanup)

    with pytest.raises(RuntimeError, match="already registered"):
        await provider.handle_async_init()

    cleanup.assert_awaited_once_with()
    assert registry.command_handlers == {_READINESS_COMMAND: incumbent}
    assert provider._unregister_private_capture_api is None


async def test_provenance_await_cannot_return_revoked_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Revocation during provenance collection cannot leave a stale match response."""
    provider, _ = _provider()
    provider._ptp_daemon = MagicMock(spec=trial_binary.TrialAsyncProcess)
    provider._trial_identity = {}
    current = True

    async def attest(*_args: object) -> dict[str, str]:
        return {"role": "ptp", "outcome": "match" if current else "unverified"}

    def provenance() -> dict[str, str]:
        nonlocal current
        current = False
        return {"status": "unavailable"}

    monkeypatch.setattr(provider_module, "is_trial_target", lambda: True)
    monkeypatch.setattr(provider, "get_players", list)
    monkeypatch.setattr(provider_module, "installer_provenance", provenance)
    monkeypatch.setattr(trial_binary, "attest_owned_child", attest)
    response = await provider.get_private_native_volume_capture_readiness()
    children = response["owned_children"]
    assert isinstance(children, list)
    assert all(child["outcome"] == "unverified" for child in children)


async def test_does_not_register_route_outside_trial_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normal profiles never expose the private capture route."""
    provider, mass = _provider()
    monkeypatch.setattr(provider_module, "is_trial_target", lambda: False)

    provider._register_private_capture_api()

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
    provider._ptp_daemon = MagicMock(spec=trial_binary.TrialAsyncProcess)
    provider._ptp_daemon.has_current_receipt.return_value = True
    provider._ptp_daemon_ready = asyncio.Event()
    provider._ptp_daemon_ready.set()
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
        "schema_version": 1,
        "snapshot_current": True,
        "ptp_ready": True,
        "prestream_ready": True,
        "playing_ready": False,
        "streams": [],
        "installer_provenance": provenance,
        "expected_sha256": {"encoding": "u32be-v1", "words": [0xAAAAAAAA] * 8},
        "startup_validation": {"check_outcome": "passed"},
        "owned_children": [{"role": "ptp", "outcome": "match"}],
    }


async def test_readiness_requires_finite_stream_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    """A binary match alone must not silently qualify an unsupported stream."""
    provider, _ = _provider()
    provider._ptp_daemon = None
    provider._trial_identity = {}
    player = MagicMock(provider=provider)
    player.device_info.mac_address = "00:00:00:00:00:01"
    stream = AirPlayStream(player)
    stream._cli_proc = MagicMock(spec=trial_binary.TrialAsyncProcess)
    stream._cli_proc.has_current_receipt.return_value = True
    player = MagicMock(stream=stream)
    monkeypatch.setattr(provider_module, "is_trial_target", lambda: True)
    monkeypatch.setattr(provider, "get_players", lambda: [player])
    monkeypatch.setattr(provider_module, "installer_provenance", dict)
    monkeypatch.setattr(
        trial_binary,
        "attest_owned_child",
        AsyncMock(return_value={"role": "stream", "outcome": "match"}),
    )
    response = await provider.get_private_native_volume_capture_readiness()
    assert "streams" in response
    assert response["playing_ready"] is False


@pytest.mark.parametrize(
    ("capture", "remote", "volume"), [(True, True, False), (False, True, True), (True, False, True)]
)
async def test_connect_snapshots_effective_child_flags_and_resets_reports(
    monkeypatch: pytest.MonkeyPatch,
    capture: bool,
    remote: bool,
    volume: bool,
) -> None:
    """The snapshot is taken from the launch environment, not later config reads."""
    provider, player, stream = _playing_provider(monkeypatch)
    player.protocol = StreamingProtocol.AIRPLAY2
    player.protocol_override = None
    values = {
        CONF_PRIVATE_NATIVE_VOLUME_CAPTURE: capture,
        CONF_EXPERIMENTAL_HOMEPOD_REMOTE: remote,
        CONF_EXPERIMENTAL_SESSION_VOLUME: volume,
    }
    provider.config = MagicMock()
    provider.config.get_value.side_effect = lambda key, default=None: values.get(key, default)
    child = _child()
    monkeypatch.setattr(child, "start", AsyncMock())
    factory = AsyncMock(return_value=child)
    monkeypatch.setattr(trial_binary, "is_trial_target", lambda: True)
    monkeypatch.setattr(trial_binary, "create_private_process", factory)
    monkeypatch.setattr(stream, "_build_cli_args", AsyncMock(return_value=["unused"]))
    monkeypatch.setattr(stream, "_check_password_preflight", lambda: None)
    monkeypatch.setattr(stream.commands_pipe, "create", AsyncMock())

    def discard_reader(coro: Coroutine[Any, Any, None]) -> MagicMock:
        """Do not run network readers in this launch-argument seam."""
        coro.close()
        return MagicMock()

    assert isinstance(provider.mass, MagicMock)
    provider.mass.create_task.side_effect = discard_reader
    await stream.connect()
    env = factory.call_args.kwargs["env"]
    request = stream.private_capture_request
    assert request is not None
    assert request.child is child
    assert request.capture_enabled == (env["CLIAIRPLAY_PRIVATE_NATIVE_VOLUME_CAPTURE"] == "1")
    assert request.homepod_remote_requested == (
        env["CLIAIRPLAY_EXPERIMENTAL_HOMEPOD_REMOTE"] == "1"
    )
    assert request.session_volume_requested == (env["CLIAIRPLAY_SESSION_VOLUME"] != "off")
    assert request.capture_enabled == (capture and remote)
    values.clear()
    assert stream.private_capture_request is request
    assert stream.private_capture_route == ("unknown", "unknown", "unknown")
    assert stream.homepod_remote_admission is None
    assert stream.private_capture_playing is False


async def test_playing_readiness_is_bound_to_capture_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finite launch and reported facts qualify this generation, not future playback."""
    provider, _, stream = _playing_provider(monkeypatch)
    response = await provider.get_private_native_volume_capture_readiness()
    assert response["playing_ready"] is True
    assert response["prestream_ready"] is False
    assert stream.native_volume_capture is not None
    assert response["streams"] == [
        {
            "generation": stream.native_volume_capture.generation,
            "current": True,
            "capture_enabled": True,
            "homepod_remote_requested": True,
            "homepod_remote_admission": True,
            "session_volume_requested": False,
            "protocol": "airplay2",
            "flow": "realtime",
            "timing": "ptp",
            "connected": True,
            "playing": True,
            "ready": True,
        }
    ]


@pytest.mark.parametrize(
    "change",
    [
        "ptp-replace",
        "ptp-exit",
        "ptp-revoke",
        "ptp-event-replace",
        "ptp-not-ready",
        "player-replace",
        "stream-replace",
        "child-replace",
        "child-exit",
        "child-revoke",
        "capture-replace",
        "request-replace",
        "stream-added",
        "stream-removed",
    ],
)
async def test_final_revalidation_rejects_changes_during_attestation(
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    """Changes after an earlier match cannot survive the last asynchronous boundary."""
    provider, player, stream = _playing_provider(monkeypatch)
    original = trial_binary.attest_owned_child
    daemon = provider._ptp_daemon
    process = stream._cli_proc
    request = stream.private_capture_request
    event = provider._ptp_daemon_ready
    assert isinstance(daemon, trial_binary.TrialAsyncProcess)
    assert isinstance(process, trial_binary.TrialAsyncProcess)
    assert request is not None
    assert event is not None

    async def attest(child: AsyncProcess, role: str) -> dict[str, SerializableType]:
        result = await original(child, role)
        if role == "stream":
            if change == "ptp-replace":
                provider._ptp_daemon = _child()
            elif change == "ptp-exit":
                assert isinstance(daemon.proc, MagicMock)
                daemon.proc.returncode = 0
            elif change == "ptp-revoke":
                daemon._trusted_receipt = None
            elif change == "ptp-event-replace":
                provider._ptp_daemon_ready = asyncio.Event()
                provider._ptp_daemon_ready.set()
            elif change == "ptp-not-ready":
                event.clear()
            elif change == "player-replace":
                monkeypatch.setattr(provider, "get_players", lambda: [MagicMock(stream=stream)])
            elif change == "stream-replace":
                player.stream = AirPlayStream(player)
            elif change == "child-replace":
                stream._cli_proc = _child()
            elif change == "child-exit":
                assert isinstance(process.proc, MagicMock)
                process.proc.returncode = 0
            elif change == "child-revoke":
                process._trusted_receipt = None
            elif change == "capture-replace":
                stream.native_volume_capture = PrivateVolumeCapture()
                stream.native_volume_capture.trust(process)
            elif change == "request-replace":
                stream.private_capture_request = replace(request)
            elif change == "stream-added":
                monkeypatch.setattr(
                    provider, "get_players", lambda: [player, MagicMock(stream=stream)]
                )
            elif change == "stream-removed":
                monkeypatch.setattr(provider, "get_players", list)
        return result

    monkeypatch.setattr(trial_binary, "attest_owned_child", attest)
    response = await provider.get_private_native_volume_capture_readiness()
    _assert_not_ready(response, changed=change != "ptp-not-ready")


def _assert_not_ready(response: dict[str, SerializableType], *, changed: bool) -> None:
    """Reject ready responses and matching receipts from a changed snapshot."""
    assert response["playing_ready"] is False
    if changed:
        assert response["snapshot_current"] is False
        children = response["owned_children"]
        assert isinstance(children, list)
        assert all(item["outcome"] == "unverified" for item in children)


@pytest.mark.parametrize(
    "change",
    [
        "capture-disabled",
        "capture-missing",
        "admission-not-requested",
        "admission-false",
        "admission-unreported",
        "session-volume-on",
        "request-missing",
        "request-other-child",
        "disconnected",
        "not-playing",
        "playing-unreported",
        "superseded",
        "stopping",
        "capture-ended",
    ],
)
async def test_flags_and_live_state_are_required(
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    """A current executable alone cannot qualify unsupported or unsafe request state."""
    provider, player, stream = _playing_provider(monkeypatch)
    request = stream.private_capture_request
    capture = stream.native_volume_capture
    assert request is not None
    assert capture is not None
    if change == "capture-disabled":
        stream.private_capture_request = replace(request, capture_enabled=False)
    elif change == "capture-missing":
        stream.native_volume_capture = None
    elif change == "admission-not-requested":
        stream.private_capture_request = replace(request, homepod_remote_requested=False)
    elif change == "admission-false":
        stream.homepod_remote_admission = False
    elif change == "admission-unreported":
        stream.homepod_remote_admission = None
    elif change == "session-volume-on":
        stream.private_capture_request = replace(request, session_volume_requested=True)
    elif change == "request-missing":
        stream.private_capture_request = None
    elif change == "request-other-child":
        stream.private_capture_request = replace(request, child=_child())
    elif change == "disconnected":
        stream._connected.clear()
    elif change == "not-playing":
        player.playback_state = PlaybackState.PAUSED
    elif change == "playing-unreported":
        stream.private_capture_playing = False
    elif change == "superseded":
        stream.superseded = True
    elif change == "stopping":
        stream._stopping = True
    elif change == "capture-ended":
        capture.end(stream._cli_proc, "stream-cleared")
    response = await provider.get_private_native_volume_capture_readiness()
    assert response["playing_ready"] is False
    rows = response["streams"]
    assert isinstance(rows, list)
    assert rows[0]["ready"] is False


@pytest.mark.parametrize(
    "route",
    [
        "protocol=raop flow=legacy timing=ntp buffered=0",
        "protocol=airplay2 flow=native timing=ntp buffered=0",
        "protocol=airplay2 flow=native timing=ptp buffered=1",
        "protocol=airplay2 flow=raop-compat timing=ptp buffered=0",
        "protocol=airplay2 flow=realtime timing=ptp buffered=0",
        "protocol=secret flow=secret timing=secret buffered=secret",
        "protocol=airplay2 flow=secret timing=ptp buffered=0",
        "protocol=raop protocol=airplay2 flow=native timing=ptp buffered=0",
        "protocol=airplay2 flow=native timing=ptp buffered=0 secret=secret",
    ],
)
async def test_unsupported_routes_are_finite_and_not_ready(
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    """Actual route reports, not configured mode or arbitrary text, gate readiness."""
    provider, _, stream = _playing_provider(monkeypatch)
    stream._parse_route_status(f"[STATUS] route {route}")
    response = await provider.get_private_native_volume_capture_readiness()
    assert response["playing_ready"] is False
    assert "secret" not in str(response)


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
