"""
Private `@MEDIA1` ownership against a real child process and real pipes.

The fixture executes a small Python child through the production `TrialAsyncProcess`
(nonce acknowledgement, pinned-digest check, receipt binding) with only the descriptor
path swapped for the host interpreter, because macOS cannot exec `/dev/fd`. The production
`AirPlayStream._private_stdout_reader`, `PrivateTransportAttempt` and `_drain` own the pipes.
Nothing leaves the host; addresses are synthetic documentation addresses.

References:
    https://docs.python.org/3/library/asyncio-subprocess.html
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from music_assistant.helpers.private_transport import PrivateTransportAttempt
from music_assistant.providers.airplay import trial_binary as trial
from music_assistant.providers.airplay.stream import AirPlayStream
from tests.providers.airplay.test_stream import _make_player

SHA = "ab" * 32
TARGET = "192.0.2.10"
ROUTES = {
    "raop": "[STATUS] route protocol=raop flow=legacy timing=ntp buffered=0\n",
    "compat": "[STATUS] route protocol=airplay2 flow=raop-compat timing=ntp buffered=0\n",
    "udp": "[STATUS] route protocol=airplay2 flow=native timing=ptp buffered=0\n",
    "buffered": "[STATUS] route protocol=airplay2 flow=native timing=ptp buffered=1\n",
}
BASE: dict[str, Any] = {
    "v": 1,
    "terminal": "stop",
    "route": "raop",
    "epoch": 1,
    "fresh_units": 3,
    "fresh_bytes": 384,
    "idle_units": 0,
    "backlog_units": 0,
    "fault": False,
    "overflow": False,
    "addr": TARGET,
    "port": 6001,
    "setup_port": 6001,
}
CHILD = textwrap.dedent(
    """
    import json, os, sys, time
    nonce = sys.argv[sys.argv.index("--attest-nonce") + 1]
    sha = os.environ["FIX_SHA"]

    def w(data):
        while data:
            data = data[os.write(1, data):]

    def wait_go():
        while sys.stdin.buffer.readline() not in (b"go\\n", b""):
            pass

    ack = {"version": 1, "kind": "current-image-match-at-ack", "nonce": nonce,
           "exec_fd_sha256": sha, "self_sha256": sha}
    w(("[ATTEST] " + json.dumps(ack) + "\\n").encode())
    exec(os.environ["FIX_PROGRAM"])
    """
)
ATTEST = b'[ATTEST] {"version":1}\n'


def rec(**over: Any) -> bytes:
    """Return one record line."""
    return b"@MEDIA1 " + json.dumps(BASE | over, separators=(",", ":")).encode() + b"\n"


def prog(*steps: str) -> str:
    """Join child program statements."""
    return "\n".join(steps)


def emit(data: bytes) -> str:
    """Return a child statement writing raw bytes."""
    return f"w({data!r})"


@pytest.fixture
def pinned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin the synthetic digest and run the host interpreter in place of the exec descriptor."""
    script = tmp_path / "child.py"
    script.write_text(CHILD)
    monkeypatch.setattr(trial, "EXPECTED_SHA", SHA)
    monkeypatch.setattr(trial, "is_trial_target", lambda: True)
    monkeypatch.setattr(trial, "descriptor_path", lambda _fd: sys.executable)
    return script


@dataclass
class Run:
    """One production-owned attempt around a real child."""

    attempt: PrivateTransportAttempt
    stream: AirPlayStream
    proc: trial.TrialAsyncProcess

    async def go(self) -> None:
        """Release the child's wait_go through its real stdin pipe."""
        assert self.proc.proc is not None
        assert self.proc.proc.stdin is not None
        self.proc.proc.stdin.write(b"go\n")
        await self.proc.proc.stdin.drain()

    async def finish(self, timeout: float = 8) -> dict[str, Any]:
        """Stop via the production attempt and return its snapshot."""
        cleanup = self.attempt.request_stop("test-stop")
        await asyncio.wait_for(cleanup, timeout)
        self.attempt.watchdog.cancel()
        await asyncio.gather(self.attempt.watchdog, return_exceptions=True)
        return dict(self.attempt.snapshot())

    @property
    def media(self) -> dict[str, Any]:
        """Return the closed media object."""
        return dict(self.attempt.media_snapshot())


async def start(
    pinned: Path,
    program: str,
    *,
    delay_reader: bool = False,
    deadlines: tuple[float, float, float] | None = None,
    started: bool = True,
) -> Run:
    """Spawn the child under a v2 attempt exactly like `AirPlayStream.connect`."""
    player = _make_player()
    player.address = TARGET
    stream: Any = AirPlayStream(player)
    stream._write_cli_command = AsyncMock(return_value=True)
    stream.commands_pipe.remove = AsyncMock()
    attempt = PrivateTransportAttempt("a" * 32, player, object(), "x", lambda: True, 2, TARGET)
    if deadlines is not None:
        now = attempt.loop.time()
        attempt.cancel_at, attempt.drain_at, attempt.observe_at = (now + d for d in deadlines)
    stream.private_attempt = attempt
    attempt.bind_stream(stream)
    attempt.bind_session(stream, stream.private_release)
    if started:
        attempt.start_generation = 1
    fd = os.open(os.devnull, os.O_RDONLY)
    proc = trial.TrialAsyncProcess(
        ["fixture", str(pinned)],
        asset_fd=fd,
        stdin=True,
        stdout=True,
        stderr=True,
        name="fixture",
        env={"FIX_SHA": SHA, "FIX_PROGRAM": program},
    )
    stream._cli_proc = proc

    async def owner() -> None:
        """Optionally let the child exit before the first stdout read."""
        if delay_reader:
            assert proc.proc is not None
            await proc.proc.wait()
        await stream._private_stdout_reader()

    await attempt.spawn(proc, owner)
    stream._stdout_reader_task = attempt.stdout_owners[proc]
    return Run(attempt, stream, proc)


def route_and(route: str, *tail: bytes) -> str:
    """Return a program: route status, wait for stop, then the given raw bytes."""
    return prog(emit(ROUTES[route].encode()), "wait_go()", *(emit(t) for t in tail))


@pytest.mark.parametrize("route", ["raop", "compat", "udp"])
async def test_final_after_release_and_stopped_is_accepted(pinned: Path, route: str) -> None:
    """The retained private owner accepts a final written after `private_release`."""
    run = await start(pinned, route_and(route, rec(route=route)))
    result = await run.finish()
    assert run.stream._stopped
    assert run.stream.player.stream is None
    assert run.stream._cli_proc is run.proc
    assert run.media == {
        "state": "complete",
        "route": route,
        "epoch": 1,
        "fresh_units": 3,
        "fresh_bytes": 384,
    }
    assert result["outcome"] == "natural-cleanup"
    assert result["cleanup_complete"] is True
    assert run.proc.proc is not None
    assert run.proc.proc.returncode == 0
    assert run.proc.has_historical_receipt()
    assert not run.proc.has_current_receipt()


@pytest.mark.parametrize(
    ("status", "record_route"),
    [
        ("buffered", "udp"),
        ("raop", "compat"),
        ("compat", "udp"),
        ("udp", "raop"),
        ("udp", "buffered"),
    ],
)
async def test_route_cross_check_against_stdout_route(
    pinned: Path, status: str, record_route: str
) -> None:
    """The receipt route must equal the separately parsed route line, per actual route."""
    run = await start(pinned, route_and(status, rec(route=record_route)))
    await run.finish()
    assert run.media["state"] == "incomplete"


async def test_missing_route_line_is_incomplete(pinned: Path) -> None:
    """A valid record with no reported route cannot qualify."""
    run = await start(pinned, prog("wait_go()", emit(rec())))
    await run.finish()
    assert run.media["state"] == "incomplete"


async def test_final_written_and_child_exited_before_first_read(pinned: Path) -> None:
    """Buffered pipe bytes survive `returncode`; EOF-time validation is historical."""
    run = await start(pinned, route_and("raop", rec()), delay_reader=True)
    await run.go()
    assert run.proc.proc is not None
    await asyncio.wait_for(run.proc.proc.wait(), 5)
    assert run.proc.returncode == 0
    assert not run.proc.has_current_receipt()
    await run.finish()
    assert run.media["state"] == "complete"


async def test_idle_additional_counts_do_not_invalidate(pinned: Path) -> None:
    """Native idle and backlog counters are additional, never invalid."""
    run = await start(pinned, route_and("udp", rec(route="udp", idle_units=4, backlog_units=2)))
    await run.finish()
    assert run.media["state"] == "complete"


@pytest.mark.parametrize(
    "tail",
    [
        pytest.param([], id="no-record"),
        pytest.param([rec(), rec()], id="two-records"),
        pytest.param([rec(), b"[STATUS] latency lead_ms=1\n"], id="extra-after-final"),
        pytest.param([rec()[:-1]], id="truncated-no-lf"),
        pytest.param([rec()[:20]], id="truncated-partial"),
        pytest.param([b"@MEDIA1 {bad\n"], id="garbled"),
        pytest.param([b"@MEDIA2 " + rec()[8:]], id="other-version"),
        pytest.param([b"@MED"], id="partial-prefix"),
        pytest.param([b"x" * 4097 + b"\n"], id="overlong-status"),
        pytest.param([b"@MEDIA1 " + b"x" * 600 + b"\n"], id="overlong-media"),
        pytest.param([b"x @MEDIA1 " + rec()[8:]], id="not-at-line-start"),
        pytest.param([rec(fault=True)], id="fault"),
        pytest.param([rec(overflow=True)], id="overflow"),
        pytest.param([rec(epoch=0)], id="epoch-0"),
        pytest.param([rec(terminal="abnormal")], id="abnormal"),
        pytest.param([rec(addr="192.0.2.77")], id="wrong-address"),
        pytest.param([rec(port=6002)], id="port-differs-from-setup"),
        pytest.param([rec(route="buffered")], id="buffered-route"),
    ],
)
async def test_bad_or_missing_receipts_are_incomplete_without_log_leak(
    pinned: Path, caplog: pytest.LogCaptureFixture, tail: list[bytes]
) -> None:
    """Each closed failure is incomplete, and no prefix or endpoint reaches any logger."""
    caplog.set_level(1)
    run = await start(pinned, route_and("raop", *tail))
    await run.finish()
    assert run.media["state"] == "incomplete"
    assert "@MEDIA" not in caplog.text
    assert TARGET not in caplog.text
    assert "192.0.2.77" not in caplog.text


async def test_total_cap_multichunk_is_incomplete_and_drained(
    pinned: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """More than 64 KiB of valid ordinary lines fails and is discarded to EOF."""
    caplog.set_level(1)
    lines = b"".join(b"s" * 999 + b"\n" for _ in range(80))
    run = await start(pinned, route_and("raop", lines, rec()))
    result = await run.finish()
    assert run.media["state"] == "incomplete"
    assert result["cleanup_complete"] is True
    assert run.stream._cli_proc is not None


async def test_ordinary_status_lines_still_dispatch(pinned: Path) -> None:
    """Ordinary stdout status handling is preserved under the private owner."""
    latency = b"[STATUS] latency lead_ms=2000 device_min_frames=1 device_max_frames=2\n"
    run = await start(pinned, route_and("raop", latency, rec()))
    await run.finish()
    assert run.stream.private_media_route == "raop"
    assert run.media["state"] == "complete"


async def test_duplicate_attest_after_final_before_eof_rejects(pinned: Path) -> None:
    """A late `[ATTEST]` revokes the held final and the child is killed exactly once."""
    run = await start(pinned, route_and("raop", rec(), ATTEST, b"x" * 200_000))
    kills = 0
    assert run.proc.proc is not None
    original = run.proc.proc.kill

    def counted() -> None:
        nonlocal kills
        kills += 1
        original()

    child: Any = run.proc.proc
    child.kill = counted
    result = await run.finish()
    assert run.media["state"] == "incomplete"
    assert kills == 1
    assert run.proc._trusted_receipt is None
    assert result["outcome"] == "forced-containment"
    assert result["cleanup_complete"] is True


@pytest.mark.parametrize("split", range(1, 8))
async def test_attest_split_across_chunks_is_rejected(pinned: Path, split: int) -> None:
    """A marker split at every byte boundary is rejected before a later final can count."""
    marker = b"[ATTEST]"
    program = prog(
        emit(ROUTES["raop"].encode()),
        "wait_go()",
        emit(marker[:split]),
        "time.sleep(0.05)",
        emit(marker[split:] + b" {}\n"),
        emit(rec()),
    )
    run = await start(pinned, program)
    await run.finish()
    assert run.media["state"] == "incomplete"
    assert run.proc._trusted_receipt is None


async def test_qualification_revocation_before_eof_rejects(pinned: Path) -> None:
    """Revoking the exact child's trust mid-run rejects its later final and EOF."""
    run = await start(pinned, route_and("raop", rec()))
    run.proc._trusted_receipt = None
    await run.finish()
    assert run.media["state"] == "incomplete"


@pytest.mark.parametrize("replace", ["child", "stream", "backend"])
async def test_replaced_owner_cannot_satisfy_attempt(pinned: Path, replace: str) -> None:
    """A replacement child, stream or backend never completes the attempt."""
    run = await start(pinned, route_and("raop", rec()))
    if replace == "child":
        run.stream._cli_proc = trial.TrialAsyncProcess(
            ["fixture"], asset_fd=os.open(os.devnull, os.O_RDONLY), stdout=True
        )
    elif replace == "stream":
        run.attempt.stream = object()
    else:
        holder: Any = run.stream
        holder.session_volume = object()
    await run.go()
    result = await run.finish()
    assert run.media["state"] == "incomplete"
    assert result["cleanup_complete"] is True


async def test_cancelled_reader_is_incomplete_and_cleanup_continues(pinned: Path) -> None:
    """A cancelled owner is reader-lost; containment still reaps within the window."""
    run = await start(pinned, route_and("raop", rec()))
    owner = run.attempt.stdout_owners[run.proc]
    owner.cancel()
    await asyncio.gather(owner, return_exceptions=True)
    result = await run.finish()
    assert run.media["state"] == "incomplete"
    assert result["cleanup_complete"] is True


async def test_forced_child_never_upgrades_even_with_valid_final(pinned: Path) -> None:
    """A child that closes stdout after a valid final, then ignores STOP, is forced-incomplete."""
    program = prog(
        emit(ROUTES["raop"].encode()),
        "wait_go()",
        emit(rec()),
        "os.close(1)",
        "time.sleep(60)",
    )
    run = await start(pinned, program, deadlines=(0.15, 0.6, 0.7))
    result = await run.finish()
    assert result["outcome"] == "forced-containment"
    assert run.proc.proc is not None
    assert run.proc.proc.returncode == -9
    assert run.media["state"] == "incomplete"


async def test_late_final_after_observation_is_incomplete(pinned: Path) -> None:
    """A record and EOF after `observe_at` cannot complete, nor repair cleanup."""
    program = prog(emit(ROUTES["raop"].encode()), "wait_go()", "time.sleep(0.9)", emit(rec()))
    run = await start(pinned, program, deadlines=(0.1, 5.0, 0.5))
    result = await run.finish()
    assert run.media["state"] == "incomplete"
    assert result["outcome"] == "incomplete"


async def test_receipt_wait_does_not_delay_force_containment(pinned: Path) -> None:
    """A silent child is killed at the drain deadline regardless of the pending receipt."""
    run = await start(
        pinned, prog(emit(ROUTES["raop"].encode()), "time.sleep(60)"), deadlines=(0.1, 0.5, 0.6)
    )
    loop = asyncio.get_running_loop()
    began = loop.time()
    result = await run.finish()
    assert loop.time() - began < 3
    assert result["outcome"] == "forced-containment"
    assert run.media["state"] == "incomplete"


async def test_pending_before_stop_then_incomplete_after_observation(pinned: Path) -> None:
    """No determination is pending; the immutable observation time latches incomplete."""
    run = await start(pinned, route_and("raop", rec()), deadlines=(5, 6, 0.4))
    assert run.media["state"] == "pending"
    await asyncio.sleep(0.45)
    assert run.media["state"] == "incomplete"
    await run.go()
    await run.finish()
    assert run.media["state"] == "incomplete"


async def test_second_start_never_completes(pinned: Path) -> None:
    """Without the single claimed START the attempt cannot complete."""
    run = await start(pinned, route_and("raop", rec()), started=False)
    await run.finish()
    assert run.media["state"] == "incomplete"


async def test_old_cancelled_reader_control_loses_the_final(
    pinned: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: the pre-change release cancelled the stdout reader and lost the final."""
    original = AirPlayStream.private_release

    async def old_release(self: AirPlayStream) -> None:
        """Cancel the stdout reader first, like the old `private_release`."""
        if self._stdout_reader_task is not None:
            self._stdout_reader_task.cancel()
            await asyncio.gather(self._stdout_reader_task, return_exceptions=True)
        await original(self)

    monkeypatch.setattr(AirPlayStream, "private_release", old_release)
    run = await start(pinned, route_and("raop", rec()))
    await run.finish()
    assert run.proc.proc is not None
    assert run.proc.proc.returncode == 0
    assert run.media["state"] != "complete"


async def test_wrapper_close_and_kill_before_eof_never_steal_stdout(pinned: Path) -> None:
    """`close`/`kill` before EOF contain the child but leave bytes to the single reader."""
    for method in ("close", "kill"):
        proc = trial.TrialAsyncProcess(
            ["fixture", str(pinned)],
            asset_fd=os.open(os.devnull, os.O_RDONLY),
            stdin=True,
            stdout=True,
            stderr=True,
            name="fixture",
            env={"FIX_SHA": SHA, "FIX_PROGRAM": prog(emit(b"hello\n"), "time.sleep(60)")},
        )
        await proc.start()
        proc.enter_receipt_mode()
        first = await proc.read_receipt_chunk(64)
        assert first == b"hello\n"
        await getattr(proc, method)()
        assert proc._stdout_left_to_reader
        assert not proc.has_current_receipt()
        assert not proc.has_historical_receipt()
        assert await proc.read_receipt_chunk(64) == b""
        assert proc.returncode == -9


async def test_wrapper_close_before_eof_discards_to_real_eof_not_early_empty(
    pinned: Path,
) -> None:
    """After a close the reader discards buffered bytes and returns b'' only at real EOF."""
    program = prog(emit(b"abc\n"), "time.sleep(0.3)", emit(b"def\n"), "time.sleep(60)")
    proc = trial.TrialAsyncProcess(
        ["fixture", str(pinned)],
        asset_fd=os.open(os.devnull, os.O_RDONLY),
        stdin=True,
        stdout=True,
        stderr=True,
        name="fixture",
        env={"FIX_SHA": SHA, "FIX_PROGRAM": program},
    )
    await proc.start()
    proc.enter_receipt_mode()
    await asyncio.sleep(0.1)
    await proc.close()
    before = proc._receipt_eof
    assert not before
    assert await proc.read_receipt_chunk(2) == b""
    assert proc._receipt_eof
    assert proc.proc is not None
    assert proc.proc.stdout is not None
    assert proc.proc.stdout.at_eof()
    assert proc.returncode == -9


async def test_wrapper_split_marker_never_exposed_and_benign_prefix_passes(
    pinned: Path,
) -> None:
    """Prefix bytes are held, then released intact when the line diverges or ends at EOF."""
    program = prog(
        emit(b"[AT"),
        "time.sleep(0.1)",
        emit(b"x\n[A"),
        "time.sleep(0.1)",
        emit(b"T"),
    )
    proc = trial.TrialAsyncProcess(
        ["fixture", str(pinned)],
        asset_fd=os.open(os.devnull, os.O_RDONLY),
        stdin=True,
        stdout=True,
        stderr=True,
        name="fixture",
        env={"FIX_SHA": SHA, "FIX_PROGRAM": program},
    )
    await proc.start()
    proc.enter_receipt_mode()
    seen = b""
    while chunk := await proc.read_receipt_chunk(1):
        seen += chunk
    assert seen == b"[ATx\n[AT"
    await proc.close()


async def test_wrapper_mode_guards(pinned: Path) -> None:
    """Receipt mode excludes every competing reader and enforces chunk bounds."""
    idle = trial.TrialAsyncProcess(["fixture"], asset_fd=os.open(os.devnull, os.O_RDONLY))
    with pytest.raises(trial.TrialBinaryError):
        idle.enter_receipt_mode()
    with pytest.raises(trial.TrialBinaryError):
        await idle.read_receipt_chunk(1)
    await idle.close()
    proc = trial.TrialAsyncProcess(
        ["fixture", str(pinned)],
        asset_fd=os.open(os.devnull, os.O_RDONLY),
        stdin=True,
        stdout=True,
        stderr=True,
        name="fixture",
        env={"FIX_SHA": SHA, "FIX_PROGRAM": "wait_go()"},
    )
    await proc.start()
    proc.enter_receipt_mode()
    with pytest.raises(trial.TrialBinaryError):
        proc.enter_receipt_mode()
    for call in (
        proc.read_stdout(),
        proc.read(10),
        proc.readexactly(1),
        proc.communicate(),
    ):
        with pytest.raises(trial.TrialBinaryError):
            await call
    for size in (0, -1, trial.MAX_RECEIPT_CHUNK + 1):
        with pytest.raises(trial.TrialBinaryError):
            await proc.read_receipt_chunk(size)
    first = asyncio.create_task(proc.read_receipt_chunk(8))
    await asyncio.sleep(0.05)
    with pytest.raises(trial.TrialBinaryError):
        await proc.read_receipt_chunk(8)
    assert proc.proc is not None
    assert proc.proc.stdin is not None
    proc.proc.stdin.write(b"go\n")
    assert await asyncio.wait_for(first, 5) == b""
    await proc.close()


async def test_first_attestation_path_is_unchanged(pinned: Path) -> None:
    """The unchanged attestation precedes any byte API: a wrong nonce never enters the mode."""
    proc = trial.TrialAsyncProcess(
        ["fixture", str(pinned)],
        asset_fd=os.open(os.devnull, os.O_RDONLY),
        stdin=True,
        stdout=True,
        name="fixture",
        env={"FIX_SHA": "cd" * 32, "FIX_PROGRAM": "pass"},
    )
    with pytest.raises(trial.TrialBinaryError):
        await proc.start()
    with pytest.raises(trial.TrialBinaryError):
        proc.enter_receipt_mode()


async def test_v1_attempt_keeps_the_original_reader_and_raw_drain(pinned: Path) -> None:
    """A v1 attempt registers no stdout owner and keeps raw draining."""
    player = _make_player()
    attempt = PrivateTransportAttempt("a" * 32, player, object(), "x", lambda: True)
    proc = trial.TrialAsyncProcess(
        ["fixture", str(pinned)],
        asset_fd=os.open(os.devnull, os.O_RDONLY),
        stdin=True,
        stdout=True,
        stderr=True,
        name="fixture",
        env={"FIX_SHA": SHA, "FIX_PROGRAM": "wait_go()"},
    )
    await attempt.spawn(proc)
    assert attempt.stdout_owners == {}
    assert attempt.snapshot()["version"] == 1
    assert "media" not in attempt.snapshot()
    await asyncio.wait_for(attempt.request_stop("done"), 8)
    attempt.watchdog.cancel()
    await asyncio.gather(attempt.watchdog, return_exceptions=True)


async def test_unscreened_reader_error_terminates_without_spinning(
    pinned: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-rejection read error ends the owner and is incomplete; it is never retried."""
    calls = 0

    async def broken(_self: trial.TrialAsyncProcess, _n: int) -> bytes:
        """Fail immediately like an unavailable reader."""
        nonlocal calls
        calls += 1
        raise trial.TrialBinaryError("Private receipt reader is unavailable")

    monkeypatch.setattr(trial.TrialAsyncProcess, "read_receipt_chunk", broken)
    run = await start(pinned, route_and("raop", rec()))
    owner = run.attempt.stdout_owners[run.proc]
    await asyncio.wait_for(asyncio.gather(owner, return_exceptions=True), 3)
    assert calls == 1
    await run.go()
    await run.finish()
    assert run.media["state"] == "incomplete"
