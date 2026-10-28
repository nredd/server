"""
Exercise private capture at the real stream, process, DACP, and diagnostics seams.

References:
    https://docs.python.org/3/library/asyncio-subprocess.html
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncGenerator, Coroutine
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.providers.airplay import provider as provider_module
from music_assistant.providers.airplay import stream as stream_module
from music_assistant.providers.airplay import trial_binary as trial
from music_assistant.providers.airplay.constants import (
    CONF_EXPERIMENTAL_HOMEPOD_REMOTE,
    CONF_PRIVATE_NATIVE_VOLUME_CAPTURE,
    StreamingProtocol,
)
from music_assistant.providers.airplay.private_volume_capture import PrivateVolumeCapture
from music_assistant.providers.airplay.provider import AirPlayProvider
from music_assistant.providers.airplay.stream import AirPlayStream
from tests.providers.airplay.dacp.conftest import (
    airplay_provider,
    config_mock,
    manifest_mock,
    mock_mass,
)
from tests.providers.airplay.dacp.helpers import build_dacp_request, replay
from tests.providers.airplay.test_private_volume_capture import _SUMMARY
from tests.providers.airplay.test_stream import _make_player
from tests.providers.airplay.test_trial_attestation import fixed_asset, install_host_spawn

__all__ = ["airplay_provider", "config_mock", "fixed_asset", "manifest_mock", "mock_mass"]

_ENV_KEY = "CLIAIRPLAY_PRIVATE_NATIVE_VOLUME_CAPTURE"
_CANARIES = (
    "PRIVATE_REMOTE_CANARY",
    "PRIVATE_PATH_CANARY",
    "PRIVATE_HEADER_CANARY",
    "PRIVATE_BODY_CANARY",
)


def _mock(value: object) -> MagicMock:
    """Narrow mocked collaborators without pretending the real provider is a mock."""
    assert isinstance(value, MagicMock)
    return value


def _make_stream(provider: AirPlayProvider) -> AirPlayStream:
    """Publish a real stream on one player with only network/controller dependencies mocked."""
    player = _make_player()
    player.provider = provider
    player.protocol_parent_id = None
    player._trial_pairing = None
    player.ignore_volume_reports = False
    player.checked_volume_enabled = False
    _mock(provider.mass).players.iter_players.return_value = [player]
    stream = AirPlayStream(player)
    player.stream = stream
    return stream


def _prepare_connect(
    stream: AirPlayStream, monkeypatch: pytest.MonkeyPatch, asset: Path
) -> list[asyncio.Task[None]]:
    """Replace network setup and schedule the production stdout/stderr readers normally."""
    tasks: list[asyncio.Task[None]] = []

    def schedule(coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """Own every real reader task so test teardown never leaves one pending."""
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    _mock(stream.mass).create_task.side_effect = schedule
    monkeypatch.setattr(stream, "_build_cli_args", AsyncMock(return_value=[str(asset)]))
    monkeypatch.setattr(stream.commands_pipe, "create", AsyncMock())
    monkeypatch.setattr(stream.commands_pipe, "remove", AsyncMock())
    monkeypatch.setattr(stream, "_write_cli_command", AsyncMock(return_value=True))
    return tasks


def _enable_capture(provider: AirPlayProvider) -> None:
    """Set strict private flags without enabling checked session-volume commands."""
    _mock(provider.config).get_value.side_effect = lambda key, default=None: (
        True
        if key in (CONF_EXPERIMENTAL_HOMEPOD_REMOTE, CONF_PRIVATE_NATIVE_VOLUME_CAPTURE)
        else default
    )


async def _finish(stream: AirPlayStream, tasks: list[asyncio.Task[None]]) -> None:
    """Reap real host children and join reader tasks even after a regression assertion."""
    if stream._cli_proc is not None:
        await stream._cli_proc.kill()
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("buffered", [False, True])
async def test_real_child_terminal_tail_survives_exit_without_live_authority(
    fixed_asset: Path,
    airplay_provider: AirPlayProvider,
    monkeypatch: pytest.MonkeyPatch,
    buffered: bool,
) -> None:
    """An acknowledged child's buffered terminal record is history, never live readiness."""
    children = install_host_spawn(monkeypatch, "capture_final", capture_summary=_SUMMARY)
    stream = _make_stream(airplay_provider)
    proc = await trial.create_private_process(
        [str(fixed_asset)], stdin=True, stdout=True, stderr=True
    )
    assert isinstance(proc, trial.TrialAsyncProcess)
    stream._cli_proc = proc
    capture = stream.native_volume_capture = PrivateVolumeCapture()
    tasks: list[asyncio.Task[None]] = []
    try:
        await proc.start()
        capture.trust(proc)
        if not buffered:
            stream._stopped = True
            tasks.append(asyncio.create_task(stream._stderr_reader()))
            proc.attach_stderr_reader(tasks[-1])
            await asyncio.sleep(0)
        await proc.write(b"x")
        assert proc._exit_watcher is not None
        await asyncio.wait_for(asyncio.shield(proc._exit_watcher), 3)
        assert proc.closed
        assert (await trial.attest_owned_child(proc, "stream"))["outcome"] == "unverified"
        if buffered:
            await asyncio.wait_for(stream._stderr_reader(), 3)
        else:
            await asyncio.wait_for(tasks[-1], 3)
        record = capture.diagnostics()
        assert record["record_valid"] is True, record
        assert record["historical_only"] is True
        assert record["current_authority"] is False
        assert not stream.connected
        assert not stream._audio_present.is_set()
        assert not stream.running
    finally:
        await _finish(stream, tasks)
    assert all(child.returncode == 0 for child in children)


async def test_detached_old_reader_cannot_mutate_reconnected_stream(
    airplay_provider: AirPlayProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blocked old reader must not handle status or teardown a replacement process epoch."""
    stream = _make_stream(airplay_provider)
    old = MagicMock(closed=False)
    replacement = MagicMock(closed=False)
    entered, release = asyncio.Event(), asyncio.Event()

    async def stderr() -> AsyncGenerator[str]:
        """Resume the old child only after the caller publishes the replacement epoch."""
        entered.set()
        await release.wait()
        yield "[STATUS] connected"
        yield _SUMMARY

    old.iter_stderr = stderr
    old_capture = PrivateVolumeCapture()
    old_capture.trust(old)
    stream._cli_proc = old
    stream.native_volume_capture = old_capture
    monkeypatch.setattr(stream.commands_pipe, "remove", AsyncMock())
    _mock(airplay_provider.mass).create_task.side_effect = lambda coro: coro.close()
    task = asyncio.create_task(stream._stderr_reader())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        new_capture = PrivateVolumeCapture()
        new_capture.trust(replacement)
        stream._cli_proc = replacement
        stream.native_volume_capture = new_capture
        release.set()
        await asyncio.wait_for(task, 3)
        assert not stream.connected, "old connected status reached the replacement actor"
        assert not stream._process_ended.is_set(), "old EOF ended the replacement actor"
        assert not stream._stopped, "old EOF stopped the replacement actor"
        _mock(stream.player).set_password_invalid.assert_not_called()
        _mock(stream.player).set_state_from_stream.assert_not_called()
        _mock(stream.player).schedule_group_rejoin.assert_not_called()
        _mock(airplay_provider.mass).players.cmd_ungroup.assert_not_called()
        _mock(airplay_provider.mass).players.cmd_set_members.assert_not_called()
        assert isinstance(stream.commands_pipe.remove, AsyncMock)
        stream.commands_pipe.remove.assert_not_awaited()
        assert new_capture.diagnostics()["terminal"] is False
        assert old_capture.diagnostics()["current_authority"] is False
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("suppression", ["ignore_volume_reports", "checked_volume_enabled"])
@pytest.mark.parametrize("ownership", ["current", "replacement", "untrusted", "revoked"])
async def test_dacp_classifies_before_suppression_and_checks_exact_receipt(
    fixed_asset: Path,
    airplay_provider: AirPlayProvider,
    monkeypatch: pytest.MonkeyPatch,
    suppression: str,
    ownership: str,
) -> None:
    """Suppressed device echoes are counted, but liveness alone never proves capture ownership."""
    install_host_spawn(monkeypatch, "valid")
    stream = _make_stream(airplay_provider)
    proc = await trial.create_private_process([str(fixed_asset)], stdin=True, stdout=True)
    stream._cli_proc = proc
    capture = stream.native_volume_capture = PrivateVolumeCapture()
    setattr(stream.player, suppression, True)
    try:
        await proc.start()
        if ownership != "untrusted":
            capture.trust(proc)
        if ownership == "replacement":
            stream._cli_proc = MagicMock(closed=False)
        elif ownership == "revoked":
            assert isinstance(proc, trial.TrialAsyncProcess)
            proc._trusted_receipt = None
            assert not proc.has_current_receipt()
            assert not proc.closed
        response = await replay(
            airplay_provider,
            build_dacp_request(
                "/ctrl-int/1/setproperty?dmcp.device-volume=-15.000000",
                active_remote=stream.active_remote_id,
            ),
        )
        record = capture.diagnostics()
        counters = record["dacp_counters"]
        assert isinstance(counters, dict)
        assert counters["device_volume"] == 1
        if ownership == "current":
            assert "incomplete" not in record
        else:
            assert record.get("incomplete") == "stale-or-unattached", record
        assert response.startswith(b"HTTP/1.0 204 No Content")
        _mock(stream.player).update_volume_from_device.assert_not_called()
        _mock(airplay_provider.mass).create_task.assert_not_called()
        if ownership == "current":
            _mock(airplay_provider.mass).create_task.side_effect = lambda coro: coro.close()
            response = await replay(
                airplay_provider,
                build_dacp_request("/ctrl-int/1/nextitem", active_remote=stream.active_remote_id),
            )
            assert response.startswith(b"HTTP/1.0 204 No Content")
            _mock(airplay_provider.mass).players.cmd_next_track.assert_called_once_with(
                stream.player.player_id
            )
    finally:
        stream._cli_proc = proc
        await proc.kill()


@pytest.mark.parametrize("level", [10, VERBOSE_LOG_LEVEL])
async def test_private_dacp_unmapped_and_malformed_requests_never_leak_raw_values(
    airplay_provider: AirPlayProvider,
    caplog: pytest.LogCaptureFixture,
    level: int,
) -> None:
    """Private selection suppresses raw logs for unknown and malformed requests too."""
    stream = _make_stream(airplay_provider)
    capture = stream.native_volume_capture = PrivateVolumeCapture()
    caplog.set_level(level, logger=airplay_provider.logger.name)
    raw = build_dacp_request(
        f"/ctrl-int/1/{_CANARIES[1]}",
        active_remote=_CANARIES[0],
        headers={"X-Private": _CANARIES[2]},
        body=_CANARIES[3],
    )
    for request in (raw, b"malformed " + raw, b"\xff" + raw):
        assert await replay(airplay_provider, request) == b""
    record = capture.diagnostics()
    counters = record["dacp_counters"]
    assert isinstance(counters, dict)
    assert counters["unmapped"] == 1
    assert counters["malformed"] == 2
    serialized = json.dumps(record) + caplog.text
    assert all(canary not in serialized for canary in _CANARIES)
    assert "AIRPLAY_DACP_CAPTURE" not in caplog.text
    _mock(stream.player).update_volume_from_device.assert_not_called()
    _mock(airplay_provider.mass).create_task.assert_not_called()


async def _initialize_without_network(
    airplay_provider: AirPlayProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Initialize real provider state while replacing service, discovery, and PTP I/O."""
    monkeypatch.setattr(provider_module, "is_trial_target", lambda: True)
    monkeypatch.setattr(provider_module, "installer_provenance", dict)
    monkeypatch.setattr(trial, "validate_trial_binary", AsyncMock(return_value={}))
    monkeypatch.setattr(trial, "preflight_current_image", AsyncMock())
    monkeypatch.setattr(provider_module, "select_free_port", AsyncMock(return_value=40000))
    monkeypatch.setattr(provider_module, "get_ip_pton", AsyncMock(return_value=b"\x7f\x00\x00\x01"))
    monkeypatch.setattr(provider_module, "SendspinBridgeManager", MagicMock())
    monkeypatch.setattr(provider_module, "AirPlayDashboards", MagicMock())
    monkeypatch.setattr(provider_module, "AsyncServiceInfo", MagicMock())
    server = MagicMock()
    server.is_serving.return_value = False
    monkeypatch.setattr(asyncio, "start_server", AsyncMock(return_value=server))
    monkeypatch.setattr(airplay_provider, "_set_pyatv_log_level", MagicMock())
    monkeypatch.setattr(airplay_provider, "_drop_unverified_password_markers", MagicMock())
    monkeypatch.setattr(airplay_provider, "_reset_auto_pinned_compat_modes", MagicMock())
    monkeypatch.setattr(airplay_provider, "_register_dacp_service", AsyncMock())
    monkeypatch.setattr(airplay_provider, "_start_ptp_daemon", AsyncMock())
    await airplay_provider.handle_async_init()


async def test_normal_stop_retains_final_in_real_diagnostics_after_stream_cleared(
    fixed_asset: Path,
    airplay_provider: AirPlayProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real stop/reader path retains bounded history after the player drops its stream."""
    children = install_host_spawn(monkeypatch, "capture_final", capture_summary=_SUMMARY)
    _enable_capture(airplay_provider)
    await _initialize_without_network(airplay_provider, monkeypatch)
    generations: list[int] = []
    records: list[Any] = []
    for _ in range(17):
        stream = _make_stream(airplay_provider)
        tasks = _prepare_connect(stream, monkeypatch, fixed_asset)
        try:
            await stream.connect()
            capture = stream.native_volume_capture
            assert capture is not None
            generations.append(capture.generation)
            await asyncio.wait_for(stream.stop(), 4)
            results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)
            assert all(
                result is None or isinstance(result, asyncio.CancelledError) for result in results
            )
            stream.player.stream = None
            diagnostics = await airplay_provider.get_diagnostics()
            history = diagnostics["private_native_volume_capture"]
            assert isinstance(history, dict)
            records = history["records"]
            assert isinstance(records, list)
            assert records[-1]["record_valid"] is True, records[-1]
            assert records[-1]["historical_only"] is True
            assert records[-1]["current_authority"] is False
            assert records[-1]["generation"] == capture.generation
            assert diagnostics["active_streams"] == 0
            serialized = json.dumps(records)
            assert stream.player.player_id not in serialized
            assert stream.active_remote_id not in serialized
            assert all(canary not in serialized for canary in _CANARIES)
        finally:
            await _finish(stream, tasks)
    assert len(records) == 16
    assert [record["generation"] for record in records] == generations[-16:]
    assert all(child.returncode == 0 for child in children)


@pytest.mark.parametrize("detached", [False, True])
@pytest.mark.parametrize("force", [False, True])
async def test_real_stop_invalidates_force_killed_capture(
    airplay_provider: AirPlayProvider,
    monkeypatch: pytest.MonkeyPatch,
    detached: bool,
    force: bool,
) -> None:
    """Explicit and fallback SIGKILL invalidate even an already detached final."""
    stream = _make_stream(airplay_provider)
    child = MagicMock(closed=False, returncode=None, write_eof=AsyncMock())

    async def terminate() -> None:
        """Model the explicit kill or graceful cleanup's bounded kill escalation."""
        child.closed = True
        child.returncode = -9

    child.kill = AsyncMock(side_effect=terminate)
    child.close = AsyncMock(side_effect=terminate)
    stream._cli_proc = child
    capture = stream.native_volume_capture = PrivateVolumeCapture()
    capture.trust(child)
    assert capture.consume_stderr(child, _SUMMARY)
    if detached:
        capture.end(child, "missing-summary")
    assert capture.diagnostics()["record_valid"] is True
    monkeypatch.setattr(stream.commands_pipe, "remove", AsyncMock())
    monkeypatch.setattr(stream, "_write_cli_command", AsyncMock(return_value=True))
    await stream.stop(force=force)
    assert capture.diagnostics()["record_valid"] is False
    assert capture.diagnostics()["incomplete"] == "force-killed"
    # A live capture demotes a forced stop to the graceful path; only the close
    # escalation itself may still kill, and that still invalidates the record.
    child.kill.assert_not_awaited()
    child.close.assert_awaited_once()
    child.write_eof.assert_awaited()


async def test_forced_stop_with_live_capture_keeps_a_clean_exit_valid(
    airplay_provider: AirPlayProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordinary (forced) stop path must still produce a valid record when the child exits."""
    stream = _make_stream(airplay_provider)
    child = MagicMock(closed=False, returncode=None, write_eof=AsyncMock())

    async def exit_cleanly() -> None:
        child.closed = True
        child.returncode = 0

    child.kill = AsyncMock()
    child.close = AsyncMock(side_effect=exit_cleanly)
    stream._cli_proc = child
    capture = stream.native_volume_capture = PrivateVolumeCapture()
    capture.trust(child)
    assert capture.consume_stderr(child, _SUMMARY)
    capture.end(child, "missing-summary")
    monkeypatch.setattr(stream.commands_pipe, "remove", AsyncMock())
    written: list[str] = []

    async def record_command(command: str) -> bool:
        written.append(command)
        return True

    monkeypatch.setattr(stream, "_write_cli_command", record_command)
    await stream.stop(force=True)
    assert written == ["ACTION=STOP"]
    child.kill.assert_not_awaited()
    child.close.assert_awaited_once()
    assert capture.diagnostics()["record_valid"] is True
    assert "incomplete" not in capture.diagnostics()


async def test_forced_stop_on_an_exited_child_keeps_its_record(
    airplay_provider: AirPlayProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child that already exited is not relabelled force-killed by a later forced stop."""
    stream = _make_stream(airplay_provider)
    child = MagicMock(closed=True, returncode=0, write_eof=AsyncMock(), kill=AsyncMock())
    child.close = AsyncMock()
    stream._cli_proc = child
    capture = stream.native_volume_capture = PrivateVolumeCapture()
    capture.trust(child)
    assert capture.consume_stderr(child, _SUMMARY)
    capture.end(child, "missing-summary")
    assert capture.diagnostics()["record_valid"] is True
    monkeypatch.setattr(stream.commands_pipe, "remove", AsyncMock())
    monkeypatch.setattr(stream, "_write_cli_command", AsyncMock(return_value=True))
    await stream.stop(force=True)
    child.kill.assert_not_awaited()
    assert capture.diagnostics()["record_valid"] is True
    assert "incomplete" not in capture.diagnostics()


async def test_demoted_stop_falls_back_to_kill_when_eof_never_drains(
    airplay_provider: AirPlayProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A paused transport that never drains cannot hang the teardown past the deadline."""
    monkeypatch.setattr(stream_module, "_CAPTURE_GRACEFUL_STOP_TIMEOUT", 0.2)
    stream = _make_stream(airplay_provider)
    child = MagicMock(closed=False, returncode=None)
    child.write_eof = AsyncMock(side_effect=lambda: asyncio.Event().wait())

    async def killed() -> None:
        child.closed = True
        child.returncode = -9

    child.kill = AsyncMock(side_effect=killed)
    child.close = AsyncMock()
    stream._cli_proc = child

    async def never() -> None:
        await asyncio.Event().wait()

    stream._stdout_reader_task = asyncio.create_task(never())
    capture = stream.native_volume_capture = PrivateVolumeCapture()
    capture.trust(child)
    assert capture.consume_stderr(child, _SUMMARY)
    monkeypatch.setattr(stream.commands_pipe, "remove", AsyncMock())
    monkeypatch.setattr(stream, "_write_cli_command", AsyncMock(return_value=True))
    await asyncio.wait_for(stream.stop(force=True), 3)
    child.kill.assert_awaited_once()
    child.close.assert_not_awaited()
    assert capture.diagnostics()["record_valid"] is False
    assert capture.diagnostics()["incomplete"] == "force-killed"


async def test_forced_stop_without_capture_still_kills(
    airplay_provider: AirPlayProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outside a capture the ordinary stop path is unchanged: immediate kill."""
    stream = _make_stream(airplay_provider)
    child = MagicMock(closed=False, returncode=None, write_eof=AsyncMock())

    async def killed() -> None:
        child.closed = True
        child.returncode = -9

    child.kill = AsyncMock(side_effect=killed)
    child.close = AsyncMock()
    stream._cli_proc = child
    stream.native_volume_capture = None
    monkeypatch.setattr(stream.commands_pipe, "remove", AsyncMock())
    monkeypatch.setattr(stream, "_write_cli_command", AsyncMock(return_value=True))
    await stream.stop(force=True)
    child.kill.assert_awaited_once()
    child.close.assert_not_awaited()


@pytest.mark.parametrize(
    ("private", "flag", "admission", "protocol", "override", "eligible"),
    [
        (True, True, True, StreamingProtocol.AIRPLAY2, None, True),
        (True, 1, True, StreamingProtocol.AIRPLAY2, None, False),
        (True, "true", True, StreamingProtocol.AIRPLAY2, None, False),
        (True, False, True, StreamingProtocol.AIRPLAY2, None, False),
        (True, True, False, StreamingProtocol.AIRPLAY2, None, False),
        (False, True, True, StreamingProtocol.AIRPLAY2, None, False),
        (True, True, True, StreamingProtocol.AIRPLAY2, StreamingProtocol.RAOP, False),
        (True, True, True, StreamingProtocol.RAOP, StreamingProtocol.AIRPLAY2, True),
    ],
)
async def test_connect_factory_gets_strict_private_effective_protocol_env(
    airplay_provider: AirPlayProvider,
    monkeypatch: pytest.MonkeyPatch,
    private: bool,
    flag: object,
    admission: bool,
    protocol: StreamingProtocol,
    override: StreamingProtocol | None,
    eligible: bool,
) -> None:
    """The real connect call explicitly overrides ambient capture without mutating its parent."""
    stream = _make_stream(airplay_provider)
    _mock(stream.player).protocol = protocol
    _mock(stream.player).protocol_override = override
    _mock(airplay_provider.config).get_value.side_effect = lambda key, default=None: (
        flag
        if key == CONF_PRIVATE_NATIVE_VOLUME_CAPTURE
        else admission
        if key == CONF_EXPERIMENTAL_HOMEPOD_REMOTE
        else default
    )
    monkeypatch.setenv(_ENV_KEY, "1")
    parent_env = dict(os.environ)
    monkeypatch.setattr(trial, "is_trial_target", lambda: private)
    monkeypatch.setattr(stream, "_build_cli_args", AsyncMock(return_value=["/fixture/cli"]))
    monkeypatch.setattr(stream.commands_pipe, "create", AsyncMock())
    child = MagicMock(closed=False, start=AsyncMock())
    private_factory = AsyncMock(return_value=child)
    packaged_factory = MagicMock(return_value=child)
    monkeypatch.setattr(trial, "create_private_process", private_factory)
    monkeypatch.setattr("music_assistant.providers.airplay.stream.AsyncProcess", packaged_factory)
    _mock(airplay_provider.mass).create_task.side_effect = lambda coro: coro.close()
    await stream.connect()
    factory = private_factory if private else packaged_factory
    assert factory.call_args.kwargs["env"][_ENV_KEY] == ("1" if eligible else "0")
    assert os.environ == parent_env
    assert (stream.native_volume_capture is not None) is eligible
    if eligible:
        assert stream.native_volume_capture is not None
        assert stream.native_volume_capture.owns(child)


@pytest.mark.parametrize("private", [False, True])
async def test_config_capture_flag_is_private_only_and_has_valid_labels(
    airplay_provider: AirPlayProvider, monkeypatch: pytest.MonkeyPatch, private: bool
) -> None:
    """Only private builds expose the default-off boolean and its truthful translated label."""
    monkeypatch.setattr(provider_module, "is_trial_target", lambda: private)
    entries = await airplay_provider.get_config_entries()
    selected = [entry for entry in entries if entry.key == CONF_PRIVATE_NATIVE_VOLUME_CAPTURE]
    assert len(selected) == int(private)
    if private:
        entry = selected[0]
        assert entry.type == ConfigEntryType.BOOLEAN
        assert entry.default_value is False
        assert entry.advanced is True
        strings = json.loads(Path(provider_module.__file__).with_name("strings.json").read_text())
        labels = strings["config_entries"][CONF_PRIVATE_NATIVE_VOLUME_CAPTURE]
        assert "private" in labels["label"].lower()
        assert "historical" in labels["description"].lower()
