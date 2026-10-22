"""
Exercise private capture after operational EOF and malformed DACP input.

References:
    https://docs.python.org/3/library/asyncio-subprocess.html
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from music_assistant.helpers import process as process_module
from music_assistant.providers.airplay import stream as stream_module
from music_assistant.providers.airplay import trial_binary as trial
from music_assistant.providers.airplay.private_volume_capture import PrivateVolumeCapture
from music_assistant.providers.airplay.provider import AirPlayProvider
from tests.providers.airplay.dacp.conftest import (
    airplay_provider,
    config_mock,
    manifest_mock,
    mock_mass,
)
from tests.providers.airplay.dacp.helpers import build_dacp_request, replay
from tests.providers.airplay.test_private_volume_capture import _SUMMARY
from tests.providers.airplay.test_private_volume_capture_integration import (
    _enable_capture,
    _finish,
    _make_stream,
    _mock,
    _prepare_connect,
)
from tests.providers.airplay.test_trial_attestation import fixed_asset, install_host_spawn

__all__ = ["airplay_provider", "config_mock", "fixed_asset", "manifest_mock", "mock_mass"]


@pytest.mark.parametrize("terminal", ["eof", "idle_timeout"])
@pytest.mark.parametrize("closed", [False, True])
async def test_capture_drains_summary_after_operational_end(
    airplay_provider: AirPlayProvider, terminal: str, closed: bool
) -> None:
    """Operational EOF must not discard the later final capture or re-enable readiness."""
    stream = _make_stream(airplay_provider)
    child = MagicMock(spec=trial.TrialAsyncProcess, closed=closed)
    child.has_current_receipt.return_value = not closed
    child.has_historical_receipt.return_value = True
    capture = stream.native_volume_capture = PrivateVolumeCapture()
    capture.trust(child)
    stream._cli_proc = child
    _mock(airplay_provider.mass).create_task.side_effect = lambda coro: coro.close()

    async def lines() -> AsyncGenerator[str]:
        """Keep the acknowledged child live while its shutdown messages are consumed."""
        yield f"[STATUS] {terminal}"
        yield "[STATUS] connected"
        yield _SUMMARY

    child.iter_stderr = lines
    await stream._stderr_reader()

    assert capture.diagnostics()["record_valid"] is True
    assert capture.diagnostics()["current_authority"] is False
    assert stream.ended_cleanly
    assert stream._stopped
    assert not stream.connected
    assert stream._process_ended.is_set()
    _mock(airplay_provider.mass).players.cmd_ungroup.assert_not_called()
    _mock(airplay_provider.mass).players.cmd_set_members.assert_not_called()


@pytest.mark.parametrize("property_name", ["dmcp.volume", "dmcp.device-volume"])
async def test_private_numeric_dacp_error_cannot_escape_or_retain_value(
    airplay_provider: AirPlayProvider,
    caplog: pytest.LogCaptureFixture,
    property_name: str,
) -> None:
    """A numeric parse failure must not expose its value through server callback errors."""
    stream = _make_stream(airplay_provider)
    child = MagicMock(spec=trial.TrialAsyncProcess, closed=False)
    child.has_current_receipt.return_value = True
    stream._cli_proc = child
    capture = stream.native_volume_capture = PrivateVolumeCapture()
    capture.trust(child)
    caplog.set_level(10, logger=airplay_provider.logger.name)
    canary = "PRIVATE_NUMERIC_VALUE_CANARY"

    response = await replay(
        airplay_provider,
        build_dacp_request(
            f"/ctrl-int/1/setproperty?{property_name}={canary}",
            active_remote=stream.active_remote_id,
        ),
    )

    assert response == b""
    assert capture.diagnostics()["record_valid"] is False
    assert "incomplete" in capture.diagnostics()
    assert canary not in repr(capture.diagnostics())
    assert canary not in caplog.text
    _mock(stream.player).update_volume_from_device.assert_not_called()
    _mock(airplay_provider.mass).create_task.assert_not_called()


@pytest.mark.parametrize("ordering", ["before_eof", "after_eof", "stop"])
async def test_late_duplicate_ack_invalidates_final_capture(
    fixed_asset: Path,
    airplay_provider: AirPlayProvider,
    monkeypatch: pytest.MonkeyPatch,
    ordering: str,
) -> None:
    """Validate the stdout tail even after the final stderr line, EOF, or graceful stop."""
    children = install_host_spawn(monkeypatch, "capture_late_duplicate", capture_summary=_SUMMARY)
    _enable_capture(airplay_provider)
    stream = _make_stream(airplay_provider)
    tasks = _prepare_connect(stream, monkeypatch, fixed_asset)
    final_consumed, release_eof = asyncio.Event(), asyncio.Event()
    published: list[PrivateVolumeCapture] = []
    monkeypatch.setattr(airplay_provider, "record_private_capture", published.append)
    try:
        await stream.connect()
        child = stream._cli_proc
        capture = stream.native_volume_capture
        assert isinstance(child, trial.TrialAsyncProcess)
        assert capture is not None
        original_iter = child.iter_stderr

        async def delayed_eof() -> AsyncGenerator[str]:
            """Pin the duplicate after summary consumption but before stderr EOF."""
            async for line in original_iter():
                yield line
                if line == _SUMMARY:
                    final_consumed.set()
            if ordering == "before_eof":
                await release_eof.wait()

        monkeypatch.setattr(child, "iter_stderr", delayed_eof)
        if ordering == "stop":
            await asyncio.wait_for(stream.stop(), 4)
        else:
            await child.write(b"x")
            await asyncio.wait_for(final_consumed.wait(), 3)
            if ordering == "after_eof":
                await asyncio.wait_for(tasks[0], 3)
                assert stream._process_ended.is_set()
                assert published == [capture]
                assert capture._child is None
                assert capture.diagnostics()["record_valid"] is False
                assert "incomplete" not in capture.diagnostics()
            await child.write(b"y")
        await asyncio.wait_for(asyncio.gather(tasks[1], return_exceptions=True), 3)
        release_eof.set()
        await asyncio.wait_for(tasks[0], 3)
        record = capture.diagnostics()
        assert record["record_valid"] is False, record
        assert record["incomplete"] == "revoked-acknowledgement", record
        assert not child.has_historical_receipt()
        assert capture._child is None
        assert not stream.running
    finally:
        release_eof.set()
        await _finish(stream, tasks)
    assert all(child.returncode is not None for child in children)


async def test_natural_exit_validates_capture_without_restoring_live_authority(
    fixed_asset: Path,
    airplay_provider: AirPlayProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real connected actor can retain a fully validated natural-exit summary."""
    children = install_host_spawn(monkeypatch, "capture_final", capture_summary=_SUMMARY)
    _enable_capture(airplay_provider)
    stream = _make_stream(airplay_provider)
    tasks = _prepare_connect(stream, monkeypatch, fixed_asset)
    try:
        await stream.connect()
        child = stream._cli_proc
        capture = stream.native_volume_capture
        assert isinstance(child, trial.TrialAsyncProcess)
        assert capture is not None
        await child.write(b"x")
        await asyncio.wait_for(asyncio.gather(*tasks), 3)
        assert child._exit_watcher is not None
        await asyncio.wait_for(asyncio.shield(child._exit_watcher), 3)
        record = capture.diagnostics()
        assert record["record_valid"] is True, record
        assert record["historical_only"] is True
        assert record["current_authority"] is False
        assert capture._child is None
        assert not child.has_current_receipt()
        assert child.has_historical_receipt()
        assert not stream.running
        assert not stream._audio_present.is_set()
    finally:
        await _finish(stream, tasks)
    assert all(child.returncode == 0 for child in children)


async def test_private_stop_bounds_stdout_validation_without_validating_raw_drain(
    fixed_asset: Path,
    airplay_provider: AirPlayProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A still-open stdout tail is incomplete, even if raw close later drains it cleanly."""
    children = install_host_spawn(monkeypatch, "capture_held_stdout", capture_summary=_SUMMARY)
    monkeypatch.setattr(stream_module, "_CAPTURE_STDOUT_TIMEOUT", 0.01)
    monkeypatch.setattr(process_module, "STREAM_RELEASE_TIMEOUT", 0.01)
    monkeypatch.setattr(process_module, "PIPE_DRAIN_TIMEOUT", 0.01)
    _enable_capture(airplay_provider)
    stream = _make_stream(airplay_provider)
    tasks = _prepare_connect(stream, monkeypatch, fixed_asset)
    try:
        await stream.connect()
        capture = stream.native_volume_capture
        assert capture is not None
        await asyncio.wait_for(stream.stop(), 3)
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)
        assert capture.diagnostics()["terminal"] is True
        assert capture.diagnostics()["record_valid"] is False
        assert capture.diagnostics()["incomplete"] == "incomplete-stdout"
        assert capture._child is None
        assert stream._cleanup_complete
        assert not stream.running
    finally:
        await _finish(stream, tasks)
    assert all(child.returncode == 0 for child in children)


async def test_revoked_child_cannot_supply_trusted_historical_summary(
    fixed_asset: Path,
    airplay_provider: AirPlayProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A revoked acknowledgement is different from a valid child's natural exit."""
    install_host_spawn(monkeypatch, "valid")
    stream = _make_stream(airplay_provider)
    child = await trial.create_private_process([str(fixed_asset)], stdin=True, stdout=True)
    assert isinstance(child, trial.TrialAsyncProcess)
    stream._cli_proc = child
    capture = stream.native_volume_capture = PrivateVolumeCapture()
    try:
        await child.start()
        capture.trust(child)
        child._trusted_receipt = None
        assert not child.has_current_receipt()
        assert stream._consume_capture_stderr(child, capture, _SUMMARY)
        assert capture.diagnostics()["record_valid"] is False
        assert capture.diagnostics()["incomplete"] == "revoked-acknowledgement"
    finally:
        await child.kill()
