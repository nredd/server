"""Regression tests for the opt-in PRE-SETUP HomePod admission policy."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import ConfigEntryType

from music_assistant.helpers.named_pipe import AsyncNamedPipeWriter
from music_assistant.helpers.process import AsyncProcess
from music_assistant.providers.airplay.constants import (
    CONF_EXPERIMENTAL_HOMEPOD_REMOTE,
    CONF_RADIO_STATION_FIRST,
)
from music_assistant.providers.airplay.stream import AirPlayStream
from tests.providers.airplay.test_provider import _make_provider
from tests.providers.airplay.test_stream import _make_player

ENV_KEY = "CLIAIRPLAY_EXPERIMENTAL_HOMEPOD_REMOTE"
MARKER = "[STATUS] homepod_remote_admission"


@pytest.mark.asyncio
async def test_admission_config_is_optional_and_independent() -> None:
    """The new provider boolean defaults off independently of station-first metadata."""
    entries = {entry.key: entry for entry in await _make_provider().get_config_entries()}
    entry = entries[CONF_EXPERIMENTAL_HOMEPOD_REMOTE]
    assert entry.type == ConfigEntryType.BOOLEAN
    assert entry.default_value is False
    assert entry.required is False
    assert entry.advanced is True
    assert entries[CONF_RADIO_STATION_FIRST].default_value is False


def _consume_task(awaitable: Any) -> MagicMock:
    """Close unused reader coroutines in spawn-only tests, like the stream fixture."""
    awaitable.close()
    return MagicMock(done=MagicMock(return_value=True))


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [False, None, True, 1, "1", "true", [], {}, "missing"])
async def test_connect_explicit_child_environment(
    value: object, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Real AsyncProcess env merging cannot leak ambient opt-in or change its parent."""
    monkeypatch.setenv(ENV_KEY, "1")
    parent_env = dict(os.environ)
    player = _make_player()
    player.provider.config.get_value.side_effect = lambda key, default=None: (
        value if key == CONF_EXPERIMENTAL_HOMEPOD_REMOTE and value != "missing" else default
    )
    player.provider.mass.create_task.side_effect = _consume_task
    stream = AirPlayStream(player)
    stream.commands_pipe = AsyncNamedPipeWriter(str(tmp_path / "commands"))
    with patch(
        "music_assistant.providers.airplay.stream.get_cli_binary",
        return_value="/fake/cliairplay",
    ):
        args = await stream._build_cli_args()
    with (
        patch.object(stream, "_build_cli_args", new=AsyncMock(return_value=args)),
        patch.object(AsyncProcess, "start", new_callable=AsyncMock),
    ):
        try:
            await stream.connect()
            assert stream._cli_proc is not None
            assert stream._cli_proc._env[ENV_KEY] == ("1" if value is True else "0")
            assert stream._cli_proc._args == args
            assert dict(os.environ) == parent_env
        finally:
            await stream.commands_pipe.remove()


@pytest.mark.asyncio
async def test_concurrent_connections_do_not_share_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Simultaneous streams keep separate explicit environments and reset selections."""
    monkeypatch.setenv(ENV_KEY, "1")
    streams = []
    for index, value in enumerate((True, False)):
        player = _make_player()
        player.provider.config.get_value.return_value = value
        player.provider.mass.create_task.side_effect = _consume_task
        stream = AirPlayStream(player)
        stream.commands_pipe = AsyncNamedPipeWriter(str(tmp_path / f"commands-{index}"))
        stream.homepod_remote_admission = True
        stream._homepod_remote_admission_reported = True
        streams.append(stream)
    with (
        patch.object(AirPlayStream, "_build_cli_args", new=AsyncMock(return_value=["binary"])),
        patch.object(AsyncProcess, "start", new_callable=AsyncMock),
    ):
        try:
            await asyncio.gather(*(stream.connect() for stream in streams))
            for stream, expected in zip(streams, ("1", "0"), strict=True):
                assert stream._cli_proc is not None
                assert stream._cli_proc._env[ENV_KEY] == expected
                assert stream.homepod_remote_admission is None
                assert stream._homepod_remote_admission_reported is False
            assert os.environ[ENV_KEY] == "1"
        finally:
            await asyncio.gather(*(stream.commands_pipe.remove() for stream in streams))


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_local_child_observes_policy_and_fifo(
    enabled: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exercise real process startup and status readers without any receiver or network."""
    monkeypatch.setenv(ENV_KEY, "1")
    player = _make_player()
    player.provider.config.get_value.return_value = enabled
    player.provider.mass.create_task.side_effect = asyncio.create_task
    stream = AirPlayStream(player)
    pipe_path = tmp_path / "commands"
    stream.commands_pipe = AsyncNamedPipeWriter(str(pipe_path))
    capabilities_seen = asyncio.Event()
    parse_capabilities = stream._parse_capabilities_status

    def record_capabilities(line: str) -> None:
        parse_capabilities(line)
        capabilities_seen.set()

    script = (
        "import os, pathlib, stat, sys; "
        f"assert stat.S_ISFIFO(pathlib.Path({str(pipe_path)!r}).stat().st_mode); "
        f"print('{MARKER} enabled=' + os.environ[{ENV_KEY!r}], flush=True); "
        "print('[STATUS] capabilities live_metadata=1', flush=True); "
        "print('[STATUS] connected', file=sys.stderr, flush=True); "
        "sys.stdin.buffer.read()"
    )
    with (
        patch.object(
            stream, "_build_cli_args", new=AsyncMock(return_value=[sys.executable, "-c", script])
        ),
        patch.object(stream, "_parse_capabilities_status", side_effect=record_capabilities),
    ):
        try:
            await stream.connect()
            await asyncio.wait_for(capabilities_seen.wait(), 5)
            await asyncio.wait_for(stream._connected.wait(), 5)
            assert stream.homepod_remote_admission is enabled
            assert stream.connected
            assert os.environ[ENV_KEY] == "1"
        finally:
            await stream.stop(force=True)
    assert not pipe_path.exists()


@pytest.mark.parametrize("enabled", ["0", "1"])
@pytest.mark.asyncio
async def test_stdout_marker_is_policy_only(enabled: str) -> None:
    """The real stdout reader records selection without declaring a connection."""
    stream = AirPlayStream(_make_player())
    stream._cli_proc = MagicMock()
    stream._cli_proc.read = AsyncMock(side_effect=[f"{MARKER} enabled={enabled}\n".encode(), b""])
    await stream._stdout_reader()
    assert stream.homepod_remote_admission is (enabled == "1")
    assert not stream.connected
    stream._handle_status_line('[STATUS] error code=connect_failed http=0 detail="rejected"')
    assert not stream.connected
    assert stream._connect_error is not None
    assert stream.homepod_remote_admission is (enabled == "1")


@pytest.mark.parametrize(
    "suffix",
    [
        "",
        " enabled=",
        " enabled=true",
        " enabled=01",
        " enabled=2",
        " enabled=1 enabled=0",
        " enabled=1 enabled=1",
        " enabled=1 other=1",
        " enabled=1 garbage",
        "x enabled=1",
    ],
)
@pytest.mark.asyncio
async def test_malformed_marker_is_not_selection(suffix: str) -> None:
    """Malformed values, duplicate fields and lookalike prefixes do not authorize SETUP."""
    stream = AirPlayStream(_make_player())
    stream._cli_proc = MagicMock()
    stream._cli_proc.read = AsyncMock(side_effect=[f"{MARKER}{suffix}\n".encode(), b""])
    await stream._stdout_reader()
    assert stream.homepod_remote_admission is None


@pytest.mark.asyncio
@pytest.mark.parametrize("capabilities", [b"", b"[STATUS] capabilities live_metadata=1\n"])
async def test_old_cli_fallback_is_not_activation(
    capabilities: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    """Missing markers leave playback connected, even with post-connect live metadata."""
    stream = AirPlayStream(_make_player())
    stream._cli_proc = MagicMock()
    capabilities_read = False

    async def read_stdout(_size: int) -> bytes:
        nonlocal capabilities_read
        if capabilities and not capabilities_read:
            capabilities_read = True
            assert "unsupported/unreported" not in caplog.text
            return capabilities
        # The capability milestone must report missing support BEFORE EOF.
        assert ("unsupported/unreported" in caplog.text) is bool(capabilities)
        return b""

    stream._cli_proc.read = AsyncMock(side_effect=read_stdout)
    stream._handle_status_line("[STATUS] connected")
    with caplog.at_level(logging.DEBUG):
        await stream._stdout_reader()
    assert stream.connected
    assert stream.homepod_remote_admission is None
    assert caplog.text.count("unsupported/unreported by CLI") == 1
    assert stream.supports_live_metadata is bool(capabilities)


@pytest.mark.asyncio
@pytest.mark.parametrize("connected_first", [False, True])
async def test_stdout_stderr_order_does_not_report_missing_marker(
    connected_first: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cross-pipe scheduling cannot beat the ordered stdout marker/capability milestone."""
    stream = AirPlayStream(_make_player())
    stream._cli_proc = MagicMock()

    stdout_read = False

    async def read_stdout(_size: int) -> bytes:
        nonlocal stdout_read
        if stdout_read:
            return b""
        stdout_read = True
        assert "unsupported/unreported" not in caplog.text
        return f"{MARKER} enabled=1\n[STATUS] capabilities live_metadata=1\n".encode()

    stream._cli_proc.read = AsyncMock(side_effect=read_stdout)
    with caplog.at_level(logging.DEBUG):
        if connected_first:
            stream._handle_status_line("[STATUS] connected")
        assert "unsupported/unreported" not in caplog.text
        await stream._stdout_reader()
        if not connected_first:
            stream._handle_status_line("[STATUS] connected")
    assert stream.connected
    assert stream.homepod_remote_admission is True
    assert stream.supports_live_metadata
    assert "unsupported/unreported" not in caplog.text
    assert "not connection or native UI success" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("first_report", ["enabled", "disabled", "capabilities", "eof"])
@pytest.mark.parametrize("connect_failed", [False, True])
async def test_first_report_is_immutable_in_stdout_reader(
    first_report: str, connect_failed: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """Duplicate, conflicting and late markers cannot rewrite a settled process report."""
    stream = AirPlayStream(_make_player())
    stream._cli_proc = MagicMock()
    initial = {
        "enabled": f"{MARKER} enabled=1\n".encode(),
        "disabled": f"{MARKER} enabled=0\n".encode(),
        "capabilities": b"[STATUS] capabilities live_metadata=1\n",
        "eof": b"",
    }[first_report]
    stream._cli_proc.read = AsyncMock(side_effect=[initial, b""] if initial else [b""])
    with caplog.at_level(logging.DEBUG):
        await stream._stdout_reader()
        expected = stream.homepod_remote_admission
        if connect_failed:
            stream._handle_status_line(
                '[STATUS] error code=connect_failed http=0 detail="rejected"'
            )
        for later in ("0", "1"):
            stream._cli_proc.read = AsyncMock(
                side_effect=[f"{MARKER} enabled={later}\n".encode(), b""]
            )
            await stream._stdout_reader()
            assert stream.homepod_remote_admission is expected
    assert stream.homepod_remote_admission is expected
    assert stream._homepod_remote_admission_reported
    assert not stream.connected
    assert (stream._connect_error is not None) is connect_failed
    assert caplog.text.count("Ignoring duplicate or late HomePod remote admission policy") == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_line", "expected"),
    [
        (f"{MARKER} enabled=0", False),
        (f"{MARKER} enabled=1", True),
        ("[STATUS] capabilities live_metadata=1", None),
    ],
)
async def test_first_report_is_immutable_before_stdout_eof(
    first_line: str, expected: bool | None
) -> None:
    """Same-pipe later markers cannot replace the first report before EOF either."""
    stream = AirPlayStream(_make_player())
    stream._cli_proc = MagicMock()
    stream._cli_proc.read = AsyncMock(
        side_effect=[
            f"{first_line}\n".encode(),
            f"{MARKER} enabled={int(expected is not True)}\n".encode(),
            b"",
        ]
    )
    await stream._stdout_reader()
    assert stream.homepod_remote_admission is expected
    assert not stream.connected


@pytest.mark.asyncio
async def test_malformed_marker_diagnostic_does_not_retain_tokens(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Normal-level diagnostics do not retain arbitrary malformed marker fields."""
    canary = "admission-secret-canary-6814"
    stream = AirPlayStream(_make_player())
    stream._cli_proc = MagicMock()
    stream._cli_proc.read = AsyncMock(
        side_effect=[f"{MARKER} enabled=1 token={canary}\n".encode(), b""]
    )
    with caplog.at_level(logging.DEBUG):
        await stream._stdout_reader()
    assert stream.homepod_remote_admission is None
    assert "Ignoring malformed HomePod remote admission policy" in caplog.text
    assert canary not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "line",
    [
        f"[DEBUG] {MARKER} enabled=1",
        f"2026-10-20 {MARKER} enabled=1",
        "homepod_remote_admission enabled=1",
        "[EVENT] homepod_remote_admission enabled=1",
    ],
)
async def test_prefixed_and_nonprotocol_markers_are_ignored(line: str) -> None:
    """Only the CLI's direct, anchored stdout machine protocol can select policy."""
    stream = AirPlayStream(_make_player())
    stream._cli_proc = MagicMock()
    stream._cli_proc.read = AsyncMock(side_effect=[f"{line}\n".encode(), b""])
    await stream._stdout_reader()
    assert stream.homepod_remote_admission is None


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", [False, True])
async def test_unreported_policy_visibility_uses_process_request(
    requested: bool, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Only an explicitly requested but unreported profile emits one visible diagnostic."""
    player = _make_player()
    player.provider.config.get_value.return_value = requested
    player.provider.mass.create_task.side_effect = _consume_task
    stream = AirPlayStream(player)
    stream.commands_pipe = AsyncNamedPipeWriter(str(tmp_path / "commands"))
    with (
        patch.object(stream, "_build_cli_args", new=AsyncMock(return_value=["binary"])),
        patch.object(AsyncProcess, "start", new_callable=AsyncMock),
    ):
        try:
            await stream.connect()
            assert stream._cli_proc is not None
            # Changing config after spawn cannot change this process's request.
            player.provider.config.get_value.return_value = not requested
            with (
                patch.object(
                    stream._cli_proc,
                    "read",
                    new=AsyncMock(side_effect=[b"[STATUS] capabilities live_metadata=1\n", b""]),
                ),
                caplog.at_level(logging.DEBUG),
            ):
                await stream._stdout_reader()
            records = [r for r in caplog.records if "unsupported/unreported by CLI" in r.message]
            assert len(records) == 1
            assert records[0].levelno == (logging.WARNING if requested else logging.DEBUG)
            assert stream.homepod_remote_admission is None
            assert stream._connect_error is None
        finally:
            await stream.commands_pipe.remove()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["preflight", "build_args", "fifo_create", "process_start"])
@pytest.mark.parametrize("requested", [False, True])
async def test_reused_stream_startup_failure_has_fresh_policy_and_cleanup(
    failure: str, requested: bool, tmp_path: Path
) -> None:
    """Failed reuse never exposes the previous selection or starts an early-failing child."""
    player = _make_player()
    player.provider.config.get_value.return_value = not requested
    player.provider.mass.create_task.side_effect = _consume_task
    stream = AirPlayStream(player)
    pipe_path = tmp_path / "commands"
    stream.commands_pipe = AsyncNamedPipeWriter(str(pipe_path))
    with (
        patch.object(stream, "_build_cli_args", new=AsyncMock(return_value=["binary"])),
        patch.object(AsyncProcess, "start", new_callable=AsyncMock),
    ):
        await stream.connect()
        assert stream._cli_proc is not None
        with patch.object(
            stream._cli_proc,
            "read",
            new=AsyncMock(side_effect=[f"{MARKER} enabled=1\n".encode(), b""]),
        ):
            await stream._stdout_reader()
    assert (stream.homepod_remote_admission, stream._homepod_remote_admission_reported) == (
        True,
        True,
    )
    await stream.commands_pipe.remove()
    stream._cli_proc = None
    player.provider.config.get_value.return_value = requested
    build_args = AsyncMock(return_value=["binary"])
    if failure == "build_args":
        build_args.side_effect = OSError("build args failed")
    original_mkfifo = os.mkfifo
    with (
        patch.object(
            stream,
            "_check_password_preflight",
            side_effect=OSError("preflight failed") if failure == "preflight" else None,
        ),
        patch.object(stream, "_build_cli_args", new=build_args),
        patch("music_assistant.providers.airplay.stream.AsyncProcess", wraps=AsyncProcess) as spawn,
        patch.object(AsyncProcess, "start", new_callable=AsyncMock) as start,
        patch.object(AsyncProcess, "kill", new_callable=AsyncMock) as kill,
        patch.object(stream.commands_pipe, "remove", wraps=stream.commands_pipe.remove) as remove,
        patch("music_assistant.helpers.named_pipe.os.mkfifo", wraps=original_mkfifo) as mkfifo,
    ):
        if failure == "fifo_create":
            mkfifo.side_effect = OSError("fifo create failed")
        if failure == "process_start":
            start.side_effect = OSError("process start failed")
        with pytest.raises(OSError, match="failed"):
            await stream.connect()
        assert stream.homepod_remote_admission is None
        assert stream._homepod_remote_admission_reported is False
        assert stream._homepod_remote_admission_requested is requested
        assert stream._cli_proc is None
        assert not stream.connected
        assert not pipe_path.exists()
        if failure in {"preflight", "build_args"}:
            spawn.assert_not_called()
            start.assert_not_awaited()
            kill.assert_not_awaited()
            remove.assert_not_awaited()
            mkfifo.assert_not_called()
        else:
            spawn.assert_called_once()
            kill.assert_awaited_once()
            remove.assert_awaited_once()
            assert stream._cleanup_complete
            if failure == "fifo_create":
                start.assert_not_awaited()
            else:
                start.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_malformed_noise_then_valid_marker_before_capabilities_is_accepted(
    enabled: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """Malformed noise cannot freeze the report before its first valid policy selection."""
    stream = AirPlayStream(_make_player())
    stream._cli_proc = MagicMock()
    stream._cli_proc.read = AsyncMock(
        side_effect=[
            f"{MARKER} enabled=bad\n".encode(),
            f"{MARKER} enabled={int(enabled)}\n".encode(),
            b"[STATUS] capabilities live_metadata=1\n",
            f"{MARKER} enabled={int(not enabled)}\n".encode(),
            b"",
        ]
    )
    with caplog.at_level(logging.DEBUG):
        await stream._stdout_reader()
    assert stream.homepod_remote_admission is enabled
    assert stream._homepod_remote_admission_reported
    assert "unsupported/unreported" not in caplog.text
    assert not any(record.levelno >= logging.WARNING for record in caplog.records)
    assert "Ignoring duplicate or late HomePod remote admission policy" in caplog.text
    assert not stream.connected


@pytest.mark.asyncio
async def test_reused_stream_resets_report_before_process_creation(tmp_path: Path) -> None:
    """Every spawn resets the report before constructing its child, not after startup."""
    player = _make_player()
    player.provider.mass.create_task.side_effect = _consume_task
    stream = AirPlayStream(player)
    stream.commands_pipe = AsyncNamedPipeWriter(str(tmp_path / "commands"))
    processes: list[AsyncProcess] = []

    def make_process(*args: Any, **kwargs: Any) -> AsyncProcess:
        assert stream.homepod_remote_admission is None
        assert stream._homepod_remote_admission_reported is False
        process = AsyncProcess(*args, **kwargs)
        processes.append(process)
        return process

    with (
        patch.object(stream, "_build_cli_args", new=AsyncMock(return_value=["binary"])),
        patch("music_assistant.providers.airplay.stream.AsyncProcess", side_effect=make_process),
        patch.object(AsyncProcess, "start", new_callable=AsyncMock),
    ):
        try:
            for enabled in (True, False):
                player.provider.config.get_value.return_value = enabled
                await stream.connect()
                assert stream._cli_proc is not None
                assert stream._cli_proc._env[ENV_KEY] == ("1" if enabled else "0")
                with patch.object(
                    stream._cli_proc,
                    "read",
                    new=AsyncMock(side_effect=[f"{MARKER} enabled={int(enabled)}\n".encode(), b""]),
                ):
                    await stream._stdout_reader()
                assert stream.homepod_remote_admission is enabled
                assert not stream.connected
            assert len(processes) == 2
            assert processes[0] is not processes[1]
        finally:
            await stream.commands_pipe.remove()
