"""
Exercise private proof gating with real harmless host processes, never Linux ELF.

References:
    https://docs.python.org/3/library/asyncio-subprocess.html
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.helpers.named_pipe import AsyncNamedPipeWriter
from music_assistant.helpers.process import descriptor_path
from music_assistant.providers.airplay import trial_binary as trial
from music_assistant.providers.airplay.stream import AirPlayStream
from tests.providers.airplay.test_stream import _make_player
from tests.providers.airplay.test_stream_session import _make_session
from tests.providers.airplay.test_trial_binary import _elf


@pytest.fixture
def fixed_asset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Validate a synthetic asset's bytes but never execute its foreign header."""
    monkeypatch.setattr(trial, "TRIAL_ROOT", tmp_path / "media")
    monkeypatch.setattr(trial, "EXPECTED_SHA", hashlib.sha256(_elf()).hexdigest())
    monkeypatch.setattr(trial, "is_trial_target", lambda: True)
    path = trial.TRIAL_ROOT / trial.EXPECTED_SHA / trial.ASSET_NAME
    path.parent.mkdir(parents=True)
    path.write_bytes(_elf())
    path.chmod(0o755)
    return path


def acknowledgement(nonce: str) -> dict[str, Any]:
    """Return the versioned protocol fixture, not a native identity claim."""
    return {
        "version": 1,
        "kind": "current-image-match-at-ack",
        "nonce": nonce,
        "exec_fd_sha256": trial.EXPECTED_SHA,
        "self_sha256": trial.EXPECTED_SHA,
    }


def install_host_spawn(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    spawned: asyncio.Event | None = None,
    *,
    capture_summary: str = "",
) -> list[asyncio.subprocess.Process]:
    """Replace only foreign execution with a real bounded host child and inherited FD."""
    actual_spawn = asyncio.create_subprocess_exec
    children: list[asyncio.subprocess.Process] = []

    async def spawn(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        """Capture exact launch arguments while retaining real pipe and reap behavior."""
        fd = int(args[args.index("--attest-fd") + 1])
        nonce = args[args.index("--attest-nonce") + 1]
        assert args[0] == descriptor_path(fd)
        assert fd in kwargs["pass_fds"]
        payload = acknowledgement(nonce)
        if mode == "wrong":
            payload["nonce"] = "0" * 32
        line = "[ATTEST] " + json.dumps(payload) + "\n"
        body = f"import os,sys,time\nassert os.pread({fd},4,0)==b'\\x7fELF'\n"
        if mode == "delay":
            body += "os.write(2,b'connected\\n')\nsys.stdin.buffer.read(1)\n"
        if mode != "silent":
            body += f"os.write(1,{line.encode()!r})\n"
            if mode == "duplicate":
                body += f"os.write(1,{line.encode()!r})\n"
            elif mode == "duplicate_overflow":
                body += "os.write(1,b'[ATTEST] '+b'x'*70000+b'\\n')\n"
            else:
                status = (
                    b"[STATUS] session_volume version=1\n"
                    if mode == "session_volume"
                    else b"[STATUS] ready\n"
                )
                body += f"os.write(1,{status!r})\n"
        if mode in ("capture_final", "capture_late_duplicate", "capture_held_stdout"):
            body += "sys.stdin.buffer.read(1)\n"
            body += "os.write(2,b'[STATUS] connected\\n')\n"
            body += f"os.write(2,{(capture_summary + chr(10)).encode()!r})\n"
            if mode == "capture_late_duplicate":
                body += "os.close(2)\nsys.stdin.buffer.read(1)\n"
                body += f"os.write(1,{line.encode()!r})\n"
            elif mode == "capture_held_stdout":
                body += "os.close(2)\ntime.sleep(0.1)\n"
        elif mode == "natural":
            body += "sys.stdin.buffer.read(1)\n"
        elif mode != "exit":
            body += "time.sleep(10)\n"
        child = await actual_spawn(sys.executable, "-c", body, **kwargs)
        children.append(child)
        if spawned is not None:
            spawned.set()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    return children


@pytest.mark.parametrize("mode", ["valid", "delay"])
@pytest.mark.parametrize("reader", ["line", "chunk"])
async def test_live_ack_precedes_status_and_readiness(
    fixed_asset: Path, monkeypatch: pytest.MonkeyPatch, mode: str, reader: str
) -> None:
    """A real inherited descriptor and ACK are required before start can release readiness."""
    spawned = asyncio.Event()
    children = install_host_spawn(monkeypatch, mode, spawned)
    proc = await trial.create_private_process([str(fixed_asset)], stdin=True, stdout=True)
    assert isinstance(proc, trial.TrialAsyncProcess)
    try:
        task = asyncio.create_task(proc.start())
        if mode == "delay":
            await asyncio.wait_for(spawned.wait(), 3)
            assert not task.done()
            assert (await trial.attest_owned_child(proc, "stream"))["outcome"] == "unverified"
            with pytest.raises(trial.TrialBinaryError, match="blocked before"):
                await proc.write(b"blocked")
            with pytest.raises(trial.TrialBinaryError, match="blocked before"):
                proc.verified_stdin_fd()
            assert proc.proc is not None
            assert proc.proc.stdin is not None
            proc.proc.stdin.write(b"x")
            await proc.proc.stdin.drain()
        await task
        receipt = await trial.attest_owned_child(proc, "stream")
        assert receipt["outcome"] == "match"
        assert receipt["proof_kind"] == "current-image-match-at-ack"
        await proc.write(b"post-ack-fixture")
        if reader == "chunk":
            assert await proc.read(0) == b""
            assert await proc.read(3) == b"[ST"
            assert await proc.read(1024) == b"ATUS] ready\n"
        else:
            assert await proc.read_stdout() == b"[STATUS] ready\n"
    finally:
        await proc.kill()
    assert children
    assert all(child.returncode is not None for child in children)


async def test_stream_cannot_attach_early_connected_reader_before_ack(
    fixed_asset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even buffered early stderr cannot create stream readiness before the proof gate."""
    spawned = asyncio.Event()
    children = install_host_spawn(monkeypatch, "delay", spawned)
    player = _make_player()
    stream = AirPlayStream(player)
    created: list[object] = []

    def consume_task(awaitable: Any) -> MagicMock:
        created.append(awaitable)
        awaitable.close()
        task = MagicMock()
        task.done.return_value = True
        return task

    player.provider.mass.create_task.side_effect = consume_task
    monkeypatch.setattr(stream, "_build_cli_args", AsyncMock(return_value=[str(fixed_asset)]))
    monkeypatch.setattr(stream.commands_pipe, "create", AsyncMock())
    monkeypatch.setattr(stream.commands_pipe, "remove", AsyncMock())
    task = asyncio.create_task(stream.connect())
    try:
        await asyncio.wait_for(spawned.wait(), 3)
        assert not task.done()
        assert not created
        assert not stream.connected
        assert stream._cli_proc is not None
        with pytest.raises(trial.TrialBinaryError, match="blocked before"):
            await stream._cli_proc.write(b"blocked")
        assert stream._cli_proc.proc is not None
        assert stream._cli_proc.proc.stdin is not None
        stream._cli_proc.proc.stdin.write(b"x")
        await stream._cli_proc.proc.stdin.drain()
        await task
        assert len(created) == 2
        assert (await trial.attest_owned_child(stream._cli_proc, "stream"))["outcome"] == "match"
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await stream._cleanup_failed_start()
    assert children
    assert all(child.returncode is not None for child in children)


async def test_private_volume_transaction_requires_current_receipt(
    fixed_asset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The private FIFO path admits session-volume IPC only after the live child ACK."""
    children = install_host_spawn(monkeypatch, "session_volume")
    player = _make_player()
    stream = AirPlayStream(player)
    stream.session_volume_requested = True
    stream.commands_pipe = MagicMock(
        write=AsyncMock(return_value=True), write_atomic=AsyncMock(return_value=True)
    )
    proc = await trial.create_private_process([str(fixed_asset)], stdin=True, stdout=True)
    assert isinstance(proc, trial.TrialAsyncProcess)
    stream._cli_proc = proc
    transaction = (
        "STREAM_VOLUME_GENERATION=1\nSTREAM_VOLUME_ORDINAL=1\n"
        "STREAM_VOLUME_LEVEL=50\nACTION=STREAM_VOLUME_APPLY"
    )
    try:
        assert await stream._write_cli_command(transaction) is False
        stream.commands_pipe.write_atomic.assert_not_awaited()

        await proc.start()
        assert stream.session_volume.handle_stdout((await proc.read_stdout()).decode().strip())
        assert stream.session_volume.supported is True
        assert await stream._write_cli_command(transaction) is True
        stream.commands_pipe.write_atomic.assert_awaited_once()
        assert stream.commands_pipe.write_atomic.await_args.args == ((transaction + "\n").encode(),)
        assert callable(stream.commands_pipe.write_atomic.await_args.kwargs["guard"])

        stream.session_volume_requested = False
        assert await stream._write_cli_command("VOLUME=50") is True
        stream.commands_pipe.write.assert_awaited_once()
        assert stream.commands_pipe.write.await_args.args == (b"VOLUME=50\n",)
        assert callable(stream.commands_pipe.write.await_args.kwargs["guard"])
    finally:
        await proc.kill()
    assert children
    assert all(child.returncode is not None for child in children)


@pytest.mark.parametrize("revocation", ["child_exit", "duplicate_ack"])
@pytest.mark.parametrize(
    ("command", "session_volume"),
    [
        ("VOLUME=50", False),
        (
            "STREAM_VOLUME_GENERATION=1\nSTREAM_VOLUME_ORDINAL=1\n"
            "STREAM_VOLUME_LEVEL=50\nACTION=STREAM_VOLUME_APPLY",
            True,
        ),
    ],
)
async def test_private_queued_fifo_command_is_dropped_when_receipt_revoked(
    fixed_asset: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    command: str,
    session_volume: bool,
    revocation: str,
) -> None:
    """A queued private command rechecks its receipt at the actual FIFO write boundary."""
    children = install_host_spawn(
        monkeypatch, "natural" if revocation == "child_exit" else "duplicate"
    )
    player = _make_player()
    stream = AirPlayStream(player)
    writer = AsyncNamedPipeWriter(str(tmp_path / "commands"))
    await writer.create()
    reader = os.open(writer.path, os.O_RDONLY | os.O_NONBLOCK)
    stream.commands_pipe = writer
    stream.session_volume_requested = session_volume
    proc = await trial.create_private_process([str(fixed_asset)], stdin=True, stdout=True)
    assert isinstance(proc, trial.TrialAsyncProcess)
    stream._cli_proc = proc
    try:
        await proc.start()
        if session_volume:
            assert stream.session_volume.handle_stdout("[STATUS] session_volume version=1")
        async with writer._write_lock:
            queued = asyncio.create_task(stream._write_cli_command(command))
            await asyncio.sleep(0)
            assert not queued.done()
            if revocation == "child_exit":
                await proc.write(b"x")
                assert proc._exit_watcher is not None
                await asyncio.wait_for(asyncio.shield(proc._exit_watcher), 3)
            else:
                with pytest.raises(trial.TrialBinaryError, match="repeated"):
                    await proc.read_stdout()
        assert await queued is False
        with pytest.raises(BlockingIOError):
            os.read(reader, 4096)

        assert await stream._write_cli_command(command) is False
        with pytest.raises(BlockingIOError):
            os.read(reader, 4096)
    finally:
        os.close(reader)
        await writer.remove()
        await proc.kill()
    assert children
    assert all(child.returncode is not None for child in children)


async def test_ffmpeg_descriptor_handoff_requires_private_ack(
    fixed_asset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The direct ffmpeg-to-stdin path cannot export an unverified child's descriptor."""
    spawned = asyncio.Event()
    children = install_host_spawn(monkeypatch, "delay", spawned)
    proc = await trial.create_private_process([str(fixed_asset)], stdin=True, stdout=True)
    assert isinstance(proc, trial.TrialAsyncProcess)
    started = asyncio.create_task(proc.start())
    session = _make_session(0, 0)
    player = session.sync_clients[0]
    stream = MagicMock(pcm_format=session.pcm_format, _cli_proc=proc)
    ffmpeg = MagicMock(start=AsyncMock())
    try:
        await asyncio.wait_for(spawned.wait(), 3)
        with (
            patch(
                "music_assistant.providers.airplay.stream_session.get_final_output_format",
                return_value=MagicMock(),
            ),
            patch(
                "music_assistant.providers.airplay.stream_session.get_media_session_id",
                return_value="fixture-session",
            ),
            patch(
                "music_assistant.providers.airplay.stream_session.FFMpeg", return_value=ffmpeg
            ) as factory,
        ):
            with pytest.raises(trial.TrialBinaryError, match="blocked before"):
                await session._start_player_ffmpeg(player, MagicMock(), stream)
            factory.assert_not_called()
            assert proc.proc is not None
            assert proc.proc.stdin is not None
            proc.proc.stdin.write(b"x")
            await proc.proc.stdin.drain()
            await started
            await session._start_player_ffmpeg(player, MagicMock(), stream)
            assert factory.call_args.kwargs["audio_output"] == proc.verified_stdin_fd()
            ffmpeg.start.assert_awaited_once()
    finally:
        if not started.done():
            started.cancel()
            with suppress(asyncio.CancelledError):
                await started
        await proc.kill()
    assert children
    assert all(child.returncode is not None for child in children)


@pytest.mark.parametrize("mode", ["wrong", "silent"])
async def test_rejected_or_timed_out_ack_reaps_child(
    fixed_asset: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """Neither an invalid nonce nor a missing frame leaves a child or proof behind."""
    children = install_host_spawn(monkeypatch, mode)
    monkeypatch.setattr(trial, "ATTEST_TIMEOUT", 0.1)
    proc = await trial.create_private_process([str(fixed_asset)], stdout=True)
    with pytest.raises(trial.TrialBinaryError, match="acknowledgement"):
        await proc.start()
    assert all(child.returncode is not None for child in children)
    assert (await trial.attest_owned_child(proc, "ptp"))["outcome"] == "unverified"
    await proc.close()


async def test_preflight_exit_is_historical_not_live_proof(
    fixed_asset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A complete zero-exit preflight is valid without manufacturing a live receipt."""
    children = install_host_spawn(monkeypatch, "exit")
    proc = await trial.create_private_process(
        [str(fixed_asset), "--attest-only"], attest_only=True, stdout=True
    )
    try:
        await proc.start()
        assert await proc.wait() == 0
        assert (await trial.attest_owned_child(proc, "ptp"))["outcome"] == "unverified"
    finally:
        await proc.close()
    assert all(child.returncode == 0 for child in children)


@pytest.mark.parametrize("reader", ["line", "chunk", "exact"])
@pytest.mark.parametrize("mode", ["duplicate", "duplicate_overflow"])
async def test_duplicate_frame_revokes_receipt_and_reaps(
    fixed_asset: Path, monkeypatch: pytest.MonkeyPatch, reader: str, mode: str
) -> None:
    """A second ACK never passes through to normal status consumers."""
    children = install_host_spawn(monkeypatch, mode)
    proc = await trial.create_private_process([str(fixed_asset)], stdout=True)
    await proc.start()
    operation = (
        proc.read(1)
        if reader == "chunk"
        else proc.readexactly(1)
        if reader == "exact"
        else proc.read_stdout()
    )
    with pytest.raises(trial.TrialBinaryError, match=r"repeated|exceeds its limit"):
        await operation
    assert all(child.returncode is not None for child in children)
    assert (await trial.attest_owned_child(proc, "stream"))["outcome"] == "unverified"
    await proc.close()


async def test_normal_exit_releases_descriptor_without_caller_close(
    fixed_asset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pairing and daemon exit monitors cannot strand the retained execution descriptor."""
    children = install_host_spawn(monkeypatch, "natural")
    proc = await trial.create_private_process([str(fixed_asset)], stdin=True, stdout=True)
    assert isinstance(proc, trial.TrialAsyncProcess)
    await proc.start()
    descriptor = proc._asset_fd
    assert descriptor is not None
    await proc.write(b"x")
    assert proc._exit_watcher is not None
    await asyncio.wait_for(asyncio.shield(proc._exit_watcher), 3)
    assert proc._asset_fd is None
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(descriptor)
    assert all(child.returncode == 0 for child in children)
    assert (await trial.attest_owned_child(proc, "pairing"))["outcome"] == "unverified"
    await proc.close()


async def test_build_check_keeps_validated_inode_during_path_swap(
    fixed_asset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Swapping and restoring the media pathname never selects an unvalidated check executable."""
    actual_spawn = asyncio.create_subprocess_exec
    observed: list[asyncio.subprocess.Process] = []

    async def swapped_spawn(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        original = fixed_asset.with_name("retained-original")
        fixed_asset.rename(original)
        fixed_asset.write_bytes(b"unexpected executable")
        fixed_asset.chmod(0o755)
        try:
            descriptor = kwargs["pass_fds"][0]
            assert args == (descriptor_path(descriptor), "--check")
            assert os.pread(descriptor, 64, 0) == _elf()
            assert os.fstat(descriptor).st_ino == original.stat().st_ino
            assert os.fstat(descriptor).st_ino != fixed_asset.stat().st_ino
            code = f"import os\nassert os.pread({descriptor},4,0)==b'\\x7fELF'\nprint('cliairplay check {trial.EXPECTED_BUILD}')\n"
            child = await actual_spawn(sys.executable, "-c", code, **kwargs)
            observed.append(child)
            return child
        finally:
            fixed_asset.unlink()
            original.rename(fixed_asset)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", swapped_spawn)
    assert await trial._check_binary(fixed_asset) == trial.EXPECTED_BUILD
    assert observed
    assert all(child.returncode == 0 for child in observed)


async def test_raw_communicate_cannot_bypass_proof(fixed_asset: Path) -> None:
    """Unsupported raw communicate never queues stdin or consumes unchecked stdout."""
    proc = await trial.create_private_process([str(fixed_asset)], stdin=True, stdout=True)
    try:
        with pytest.raises(trial.TrialBinaryError, match="verified stream methods"):
            await proc.communicate(input=b"unverified")
    finally:
        await proc.close()


async def test_cancelled_ack_reaps_real_child(
    fixed_asset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation during the bounded handshake owns cleanup rather than orphaning it."""
    spawned = asyncio.Event()
    children = install_host_spawn(monkeypatch, "silent", spawned)
    proc = await trial.create_private_process([str(fixed_asset)], stdout=True)
    task = asyncio.create_task(proc.start())
    await asyncio.wait_for(spawned.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert children
    assert all(child.returncode is not None for child in children)
    await proc.close()


async def test_release_never_closes_a_reused_descriptor(fixed_asset: Path) -> None:
    """Closing a failed launch twice cannot close a later unrelated file descriptor."""
    fd = os.open(fixed_asset, os.O_RDONLY)
    proc = trial.TrialAsyncProcess([str(fixed_asset)], asset_fd=fd, stdout=True)
    await proc.close()
    unrelated = os.open(fixed_asset, os.O_RDONLY)
    try:
        await proc.close()
        assert os.fstat(unrelated).st_size == fixed_asset.stat().st_size
    finally:
        os.close(unrelated)


@pytest.mark.parametrize(
    "change", ["boolean", "float", "nonce", "exec", "self", "kind", "duplicate"]
)
def test_ack_parser_rejects_ambiguous_authority(change: str) -> None:
    """Only exact typed, unique version/nonce/image fields can establish authority."""
    nonce = "1" * 32
    payload = acknowledgement(nonce)
    if change == "boolean":
        payload["version"] = True
    elif change == "float":
        payload["version"] = 1.0
    elif change == "nonce":
        payload["nonce"] = "2" * 32
    elif change == "exec":
        payload["exec_fd_sha256"] = "0" * 64
    elif change == "self":
        payload["self_sha256"] = "0" * 64
    elif change == "kind":
        payload["kind"] = "launch-history"
    raw = json.dumps(payload)
    if change == "duplicate":
        raw = raw[:-1] + ', "version": 1}'
    with pytest.raises(trial.TrialBinaryError, match="acknowledgement"):
        trial._parse_attestation_line(("[ATTEST] " + raw + "\n").encode(), nonce)
