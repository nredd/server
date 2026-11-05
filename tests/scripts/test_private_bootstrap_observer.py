"""Offline tests for the bounded private DEV bootstrap observer."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from collections import deque
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from scripts import private_bootstrap_observer as observer
from scripts import private_dev_lifecycle as lifecycle

SOURCE = observer.EXPECTED_SOURCE
# First line of the canonical add-on entrypoint (music-assistant/home-assistant-addon,
# `music_assistant_dev/entrypoint.sh`) when `server_repo` is `nredd/server@<sha>`.
ANCHOR = f"Building from source using server_repo: nredd/server@{SOURCE}"
# Real `music_assistant/mass.py` startup line (the only runtime marker).
STARTED = (
    "Starting Music Assistant Server (abc123) version 2.9.0 - HA add-on: True - Safe mode: False"
)


class Clock:
    """Monotonic test clock advanced only through the observer sleep seam."""

    def __init__(self) -> None:
        """Start from zero for deterministic watchdog-budget tests."""
        self.value = 0.0

    def now(self) -> float:
        """Return the current monotonic test time."""
        return self.value

    def sleep(self, seconds: float) -> None:
        """Advance the test clock without real waiting."""
        self.value += seconds


class SequenceReader:
    """Return cumulative sanitized synthetic logs and record supplied deadlines."""

    first_cursor: str | None = None

    def __init__(self, entries: list[bytes]) -> None:
        """Keep one or more complete current-log snapshots."""
        self.entries = deque(entries)
        self.deadlines: list[float] = []

    def read(self, deadline: float) -> bytes:
        """Return the next fixture, retaining the final snapshot for later polls."""
        self.deadlines.append(deadline)
        result = self.entries.popleft()
        if not self.entries:
            self.entries.append(result)
        return result


class DelayedReader:
    """Return one synthetic log only after consuming the supplied fake-clock budget."""

    first_cursor: str | None = None

    def __init__(self, clock: Clock, delay: float, log: bytes) -> None:
        """Keep a clock seam, blocking duration and one complete log snapshot."""
        self.clock = clock
        self.delay = delay
        self.log = log

    def read(self, deadline: float) -> bytes:
        """Consume the simulated blocking duration before returning the fixed snapshot."""
        self.clock.sleep(self.delay)
        return self.log


class SequenceProbe:
    """Return only synthetic fixed-endpoint readiness values."""

    def __init__(self, values: list[bool]) -> None:
        """Keep a finite readiness sequence."""
        self.values = deque(values)
        self.deadlines: list[float] = []

    def ready(self, deadline: float) -> bool:
        """Return the next readiness value and retain the final value."""
        self.deadlines.append(deadline)
        result = self.values.popleft()
        if not self.values:
            self.values.append(result)
        return result


def baseline(log: bytes) -> observer.LogBaseline:
    """Create one safe pre-start baseline from a synthetic log fixture."""
    return observer.LogBaseline(len(log), hashlib.sha256(log).hexdigest())


def current(*lines: str) -> bytes:
    """Build sanitized synthetic log bytes with no production identities or credentials."""
    return ("\n".join(lines) + "\n").encode()


def test_stale_prior_provider_traceback_does_not_override_current_install() -> None:
    """Only errors after the new exact-source anchor belong to the observed boot."""
    old = current("Traceback TrialBinaryError: obsolete asset")
    fresh = old + current(
        ANCHOR,
        "aiolibdatachannel git update begun",
    )
    clock = Clock()
    result = observer.observe(
        SequenceReader([fresh]),
        SequenceProbe([False]),
        baseline(old),
        observer.CLEANUP_RESERVE + 10.0,
        now=clock.now,
        sleep=clock.sleep,
    )
    assert result.phase is observer.BootstrapPhase.FETCHING_DEPS
    assert result.current_boot is True
    assert result.api_ready is False
    assert result.budget_remaining is False


def test_multibyte_baseline_does_not_shift_the_current_boot_boundary() -> None:
    """A byte-positioned anchor excludes a stale error after non-ASCII baseline text."""
    old = current("π Traceback TrialBinaryError: stale")
    fresh = old + current(
        ANCHOR,
        "aiolibdatachannel git update begun",
    )
    clock = Clock()
    result = observer.observe(
        SequenceReader([fresh]),
        SequenceProbe([False]),
        baseline(old),
        observer.CLEANUP_RESERVE + 3.0,
        now=clock.now,
        sleep=clock.sleep,
    )
    assert result.phase is observer.BootstrapPhase.FETCHING_DEPS


def test_delayed_install_reaches_api_ready_without_resetting_budget() -> None:
    """A cold dependency install may progress to API readiness inside the fixed budget."""
    old = current("previous boot complete")
    install = old + current(ANCHOR, "aiolibdatachannel git update begun")
    runtime = install + current("Installed music-assistant", STARTED)
    clock = Clock()
    reader = SequenceReader([install, runtime])
    probe = SequenceProbe([False, True])
    result = observer.observe(
        reader,
        probe,
        baseline(old),
        observer.CLEANUP_RESERVE + 10.0,
        now=clock.now,
        sleep=clock.sleep,
    )
    assert result.phase is observer.BootstrapPhase.API_READY
    assert result.current_boot is True
    assert clock.value == 2 * observer.POLL_INTERVAL
    assert reader.deadlines == [10.0] * 3
    assert probe.deadlines == [10.0] * 2


def test_same_pin_prior_boot_is_not_accepted_without_a_new_anchor() -> None:
    """A healthy old boot with the same pin cannot satisfy a later boot observation."""
    old = current(ANCHOR, STARTED)
    clock = Clock()
    result = observer.observe(
        SequenceReader([old]),
        SequenceProbe([True]),
        baseline(old),
        observer.CLEANUP_RESERVE + 10.0,
        now=clock.now,
        sleep=clock.sleep,
    )
    assert result.phase is observer.BootstrapPhase.UNKNOWN
    assert result.current_boot is False
    assert result.api_ready is False


def test_api_response_before_current_runtime_is_not_boot_readiness() -> None:
    """A stale listening API cannot turn an incomplete new install into success."""
    old = current("old boot")
    log = old + current(ANCHOR, "fetching dependencies")
    clock = Clock()
    result = observer.observe(
        SequenceReader([log]),
        SequenceProbe([True]),
        baseline(old),
        observer.CLEANUP_RESERVE + 3.0,
        now=clock.now,
        sleep=clock.sleep,
    )
    assert result.phase is observer.BootstrapPhase.FETCHING_DEPS
    assert result.api_ready is False


def test_explicit_current_error_is_failure() -> None:
    """A recognized error after the current anchor is safely classified as current failure."""
    old = current("old boot")
    log = old + current(ANCHOR, "Traceback TrialBinaryError: rejected")
    clock = Clock()
    result = observer.observe(
        SequenceReader([log]),
        SequenceProbe([False]),
        baseline(old),
        observer.CLEANUP_RESERVE + 10.0,
        now=clock.now,
        sleep=clock.sleep,
    )
    assert result.phase is observer.BootstrapPhase.FAILURE
    assert result.current_boot is True


def test_rotation_or_truncation_fails_unknown_without_reusing_history() -> None:
    """A rotated log stream invalidates an anchor rather than treating prior bytes as current."""
    old = current("old boot")
    anchored = old + current(ANCHOR)
    clock = Clock()
    result = observer.observe(
        SequenceReader([anchored, current("rotated log")]),
        SequenceProbe([False, False]),
        baseline(old),
        observer.CLEANUP_RESERVE + 10.0,
        now=clock.now,
        sleep=clock.sleep,
    )
    assert result.phase is observer.BootstrapPhase.UNKNOWN
    assert result.log_continuity is False


def test_budget_exhaustion_keeps_cleanup_reserve() -> None:
    """Observer time is capped by watchdog remaining time less the fixed cleanup reserve."""
    old = current("old boot")
    log = old + current(ANCHOR, "fetching dependencies")
    clock = Clock()
    reader = SequenceReader([log])
    result = observer.observe(
        reader,
        SequenceProbe([False]),
        baseline(old),
        observer.CLEANUP_RESERVE + 5.0,
        now=clock.now,
        sleep=clock.sleep,
    )
    assert result.phase is observer.BootstrapPhase.FETCHING_DEPS
    assert result.budget_remaining is False
    assert clock.value == 5.0
    assert max(reader.deadlines) == 5.0


def test_result_redacts_logs_tokens_and_identities() -> None:
    """The result schema contains only finite booleans and phase strings."""
    result: dict[str, Any] = asdict(
        observer.BootstrapObservation(
            observer.BootstrapPhase.FAILURE, True, True, False, True, True
        )
    )
    rendered = str(result)
    assert set(result) == {
        "phase",
        "expected_source_seen",
        "current_boot",
        "api_ready",
        "budget_remaining",
        "log_continuity",
    }
    assert "token" not in rendered
    assert "Traceback" not in rendered
    assert "192.168" not in rendered


def test_blocking_log_read_cannot_produce_a_late_boot_result() -> None:
    """A blocking read that returns after the deadline is fail-closed, never readiness evidence."""
    old = current("old boot")
    log = old + current(ANCHOR, "fetching dependencies")
    clock = Clock()
    result = observer.observe(
        DelayedReader(clock, 4.0, log),
        SequenceProbe([True]),
        baseline(old),
        observer.CLEANUP_RESERVE + 3.0,
        now=clock.now,
        sleep=clock.sleep,
    )
    assert result.phase is observer.BootstrapPhase.UNKNOWN
    assert result.current_boot is False


def test_every_log_read_receives_the_same_absolute_deadline() -> None:
    """Every log read receives the same absolute deadline instead of a rolling wait budget."""
    old = current("old boot")
    log = old + current(ANCHOR, "fetching dependencies")
    clock = Clock()
    reader = SequenceReader([log])
    observer.observe(
        reader,
        SequenceProbe([False]),
        baseline(old),
        observer.CLEANUP_RESERVE + 3.0,
        now=clock.now,
        sleep=clock.sleep,
    )
    assert reader.deadlines == [3.0, 3.0]


def test_watchdog_deadline_requires_the_live_matching_lease(tmp_path: Path) -> None:
    """A future watchdog file alone cannot grant the observer a cleanup budget."""
    snapshot = lifecycle.Snapshot(
        "123e4567-e89b-12d3-a456-426614174000",
        "a" * 64,
        time.time(),
        ("core_ssh", lifecycle.DEV),
        "2.11.0.dev",
        False,
    )
    run_dir = lifecycle.save_snapshot(tmp_path, snapshot)
    lifecycle._write_json_exclusive(
        run_dir / "watchdog.json",
        {
            "run_id": snapshot.run_id,
            "nonce": snapshot.nonce,
            "deadline": time.time() + 180.0,
            "monotonic_deadline": time.monotonic() + 180.0,
        },
    )
    with pytest.raises(observer.ObserverError, match=r"unavailable|not live"):
        observer._watchdog_deadline(tmp_path, snapshot.run_id)
    lease = lifecycle._acquire_watchdog_lease(tmp_path)
    try:
        os.write(lease, json.dumps({"run_id": snapshot.run_id, "nonce": snapshot.nonce}).encode())
        assert observer._watchdog_deadline(tmp_path, snapshot.run_id) > time.monotonic()
    finally:
        os.close(lease)


def test_cli_failure_does_not_echo_lifecycle_exception(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An invalid lifecycle run produces the fixed refusal line, never its untrusted value."""
    monkeypatch.setenv("SUPERVISOR_TOKEN", "private-token")
    assert (
        observer.main(["observe", "--run", "token-like-run", "--baseline-file", "/nonexistent"])
        == 1
    )
    captured = capsys.readouterr()
    assert captured.err == "Bootstrap observer refused\n"
    assert "token-like-run" not in captured.err


@pytest.mark.parametrize(
    "baseline_data", [observer.LogBaseline(0, "invalid"), observer.LogBaseline(-1, "0" * 64)]
)
def test_invalid_baseline_is_refused(baseline_data: observer.LogBaseline) -> None:
    """Malformed safe-boundary input cannot silently expand an observation window."""
    with pytest.raises(observer.ObserverError, match="baseline"):
        observer.observe(
            SequenceReader([b""]),
            SequenceProbe([False]),
            baseline_data,
            10.0,
            now=lambda: 0.0,
            sleep=lambda _seconds: None,
        )


def test_lease_loss_mid_run_fails_unknown() -> None:
    """A torn-down owner run must not keep yielding bootstrap results at the next poll."""
    log = current("start")
    reader = SequenceReader([log, log + current(ANCHOR)])
    clock = Clock()
    leases = iter([True, False])
    result = observer.observe(
        reader,
        SequenceProbe([False]),
        baseline(log),
        1000.0,
        now=clock.now,
        sleep=clock.sleep,
        lease_live=lambda: next(leases),
    )
    assert result.phase is observer.BootstrapPhase.UNKNOWN
    assert not result.log_continuity
    assert len(reader.deadlines) == 1


def test_bool_baseline_size_is_refused() -> None:
    """`True` is an int subclass but not a byte length."""
    with pytest.raises(observer.ObserverError, match="baseline"):
        observer.observe(
            SequenceReader([b""]),
            SequenceProbe([False]),
            observer.LogBaseline(True, "0" * 64),
            10.0,
            now=lambda: 0.0,
            sleep=lambda _seconds: None,
        )


def test_installed_marker_is_source_install_and_fetch_is_deps() -> None:
    """Phase names follow the evidence they describe."""
    assert observer._classify(b"Fetching wheel\n") is observer.BootstrapPhase.FETCHING_DEPS
    assert (
        observer._classify(b"Installed music-assistant\n") is observer.BootstrapPhase.SOURCE_INSTALL
    )


class _Completed:
    """Minimal `subprocess.CompletedProcess` stand-in."""

    def __init__(self, returncode: int, stdout: bytes) -> None:
        """Keep the exit code and captured stdout."""
        self.returncode = returncode
        self.stdout = stdout


@pytest.mark.parametrize(
    ("returncode", "stdout"), [(63, b""), (22, b"error"), (0, b"x" * (observer.MAX_LOG_BYTES + 1))]
)
def test_log_reader_fails_closed_without_leaking_token(
    monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: bytes
) -> None:
    """Non-200, oversize and curl failures are sanitized errors; the token stays off argv."""
    seen: dict[str, Any] = {}

    def run(argv: list[str], **kwargs: Any) -> _Completed:
        seen["argv"] = argv
        seen["input"] = kwargs["input"]
        return _Completed(returncode, stdout)

    monkeypatch.setattr(subprocess, "run", run)
    reader = observer.SupervisorLogReader("a" * 40)
    with pytest.raises(observer.ObserverError):
        reader.read(time.monotonic() + 5)
    assert not any("a" * 40 in arg for arg in seen["argv"])
    assert seen["input"].startswith(b"fail\n")


@pytest.mark.parametrize(
    "stdout", [b'{"a":1}\n500', b"[]\n200", b"not-json\n200", b"{}", b"x" * 5000 + b"\n200"]
)
def test_api_probe_rejects_bad_responses(monkeypatch: pytest.MonkeyPatch, stdout: bytes) -> None:
    """Only a bounded JSON object with HTTP 200 is api-ready."""
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: _Completed(0, stdout))
    assert not observer.LoopbackApiProbe().ready(time.monotonic() + 5)


def test_api_probe_accepts_json_object(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bounded JSON object with 200 is ready."""
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: _Completed(0, b'{"a":1}\n200'))
    assert observer.LoopbackApiProbe().ready(time.monotonic() + 5)


def test_lease_rejects_other_owner_and_unsafe_mode(tmp_path: Path) -> None:
    """Another run's lease or a group-readable lock file is refused."""
    lock = tmp_path / "watchdog.lock"
    lock.write_text(json.dumps({"run_id": "r", "nonce": "n"}))
    lock.chmod(0o666)
    with pytest.raises(observer.ObserverError, match="unsafe"):
        observer._require_live_lease(tmp_path, "r", "n")
    lock.chmod(0o600)
    with pytest.raises(observer.ObserverError, match="not live"):
        observer._require_live_lease(tmp_path, "r", "n")
    assert not observer._lease_is_live(tmp_path, "r")


class BlockingProbe:
    """Record invocation and advance the fake clock while "blocking"."""

    def __init__(self, clock: Clock, jump: float) -> None:
        """Keep the clock to advance and the blocking duration."""
        self.clock = clock
        self.jump = jump
        self.calls = 0

    def ready(self, deadline: float) -> bool:
        """Block, then report a ready API."""
        self.calls += 1
        self.clock.value += self.jump
        return True


def _api_ready_run(
    lease_after: bool, clock_jump: float
) -> tuple[observer.BootstrapObservation, BlockingProbe]:
    """Reach `runtime` on the FIRST poll so only the post-probe recheck can intervene."""
    clock = Clock()
    log = current("start")
    runtime = log + current(ANCHOR, STARTED)
    probe = BlockingProbe(clock, clock_jump)
    leases = iter([True, lease_after])
    result = observer.observe(
        SequenceReader([runtime]),
        probe,
        baseline(log),
        1000.0,
        now=clock.now,
        sleep=clock.sleep,
        lease_live=lambda: next(leases),
    )
    return result, probe


def test_lease_dying_during_api_probe_is_unknown() -> None:
    """A probe that blocks while the owner tears down must not report api-ready."""
    result, probe = _api_ready_run(False, 0.0)
    assert probe.calls == 1
    assert result.phase is observer.BootstrapPhase.UNKNOWN
    result, probe = _api_ready_run(True, 0.0)
    assert probe.calls == 1
    assert result.phase is observer.BootstrapPhase.API_READY


def test_deadline_expiring_during_api_probe_is_not_ready() -> None:
    """A probe returning after the absolute deadline cannot be a ready observation."""
    result, probe = _api_ready_run(True, 10_000.0)
    assert probe.calls == 1
    assert result.phase is observer.BootstrapPhase.UNKNOWN
    assert not result.api_ready


class FakeJournal:
    """Emulate Supervisor's journal-backed add-on log endpoint, including its 100-line tail."""

    def __init__(self, count: int) -> None:
        """Start with `count` historical entries `c0..`."""
        self.entries = [(f"c{i}", f"old line {i}") for i in range(count)]
        self.next_id = count

    def append(self, *lines: str) -> None:
        """Append new entries."""
        for line in lines:
            self.entries.append((f"c{self.next_id}", line))
            self.next_id += 1

    def rotate(self, drop: int) -> None:
        """Drop the oldest entries as journald vacuuming would."""
        del self.entries[:drop]

    def run(self, _argv: list[str], **kwargs: Any) -> _Completed:
        """Serve the requested Range window with an `X-First-Cursor` response header."""
        config = kwargs["input"].decode()
        assert "fail" in config.splitlines()
        spec = next(
            line.split("entries=")[1].rstrip('"')
            for line in config.splitlines()
            if "Range:" in line
        )
        cursor, skip, count = spec.split(":")
        assert skip, "explicit skip field required"
        if cursor == "" and skip:
            chosen = self.entries[int(skip) - 1 :][: int(count)]
        else:
            ids = [c for c, _ in self.entries]
            start = ids.index(cursor) if cursor in ids else 0
            chosen = self.entries[start:][: int(count)]
        body = "".join(f"{line}\n" for _, line in chosen).encode()
        head = (
            f"HTTP/1.1 200 OK\r\nX-First-Cursor: {chosen[0][0]}\r\n\r\n"
            if chosen
            else ("HTTP/1.1 200 OK\r\n\r\n")
        )
        return _Completed(0, head.encode() + body)


def test_baseline_survives_a_full_sliding_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hundreds of new lines must not push the baseline out of an already-full 100-line tail."""
    journal = FakeJournal(300)
    monkeypatch.setattr(subprocess, "run", journal.run)
    token = "a" * 40
    base = observer.capture_baseline(observer.SupervisorLogReader(token), time.monotonic() + 5)
    assert base.cursor == "c298"
    journal.append(ANCHOR)
    journal.append(*[f"Fetching wheel {i}" for i in range(400)])
    journal.append(STARTED)
    reader = observer.SupervisorLogReader(token, base.cursor)
    result = observer.observe(
        reader,
        SequenceProbe([True]),
        base,
        time.monotonic() + 1000,
    )
    assert result.phase is observer.BootstrapPhase.API_READY
    assert result.log_continuity


def test_rotated_baseline_cursor_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Journal vacuuming past the baseline entry fails closed instead of misreading history."""
    journal = FakeJournal(50)
    monkeypatch.setattr(subprocess, "run", journal.run)
    token = "a" * 40
    base = observer.capture_baseline(observer.SupervisorLogReader(token), time.monotonic() + 5)
    journal.rotate(49)
    journal.append(ANCHOR)
    result = observer.observe(
        observer.SupervisorLogReader(token, base.cursor),
        SequenceProbe([True]),
        base,
        time.monotonic() + 1000,
    )
    assert result.phase is observer.BootstrapPhase.UNKNOWN


def test_log_cursor_header_is_validated() -> None:
    """A hostile cursor cannot be spliced into a later Range header."""
    with pytest.raises(observer.ObserverError):
        observer._first_cursor(b"HTTP/1.1 200 OK\r\nX-First-Cursor: a b")
    with pytest.raises(observer.ObserverError):
        observer.SupervisorLogReader("a" * 40, 'bad"cursor')


def test_entry_limit_truncation_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """A body that may have been truncated at the entry cap is refused, not read as a prefix."""
    journal = FakeJournal(3)
    monkeypatch.setattr(subprocess, "run", journal.run)
    token = "a" * 40
    base = observer.capture_baseline(observer.SupervisorLogReader(token), time.monotonic() + 5)
    journal.append(*[f"line {i}" for i in range(observer.MAX_LOG_ENTRIES)])
    with pytest.raises(observer.ObserverError, match="entry limit"):
        observer.SupervisorLogReader(token, base.cursor).read(time.monotonic() + 5)


def test_failure_after_lease_loss_is_unknown() -> None:
    """A failure line read just as the owner tears down is not a result."""
    log = current("start")
    bad = log + current(ANCHOR, "Traceback boom")
    reader = SequenceReader([bad])
    leases = iter([True, False])
    result = observer.observe(
        reader,
        SequenceProbe([False]),
        baseline(log),
        1000.0,
        now=Clock().now,
        sleep=lambda _s: None,
        lease_live=lambda: next(leases),
    )
    assert len(reader.deadlines) == 1
    assert result.phase is observer.BootstrapPhase.UNKNOWN
    live = observer.observe(
        SequenceReader([bad]),
        SequenceProbe([False]),
        baseline(log),
        1000.0,
        now=Clock().now,
        sleep=lambda _s: None,
    )
    assert live.phase is observer.BootstrapPhase.FAILURE


class _CliReader:
    """Stand in for the Supervisor reader in CLI tests."""

    def __init__(self, *_args: Any) -> None:
        """Ignore the token/cursor and expose a sensitive-looking cursor."""
        self.first_cursor: str | None = "s=SECRETMACHINE;i=1;b=SECRETBOOT"

    def read(self, _deadline: float) -> bytes:
        """Return a fixed two-line log."""
        return b"line one\nline two\n"


def test_baseline_cli_writes_owner_only_file_and_prints_nothing_sensitive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The raw cursor lives only in a 0600 file; stdout is a fixed line, argv never has it."""
    monkeypatch.setenv("SUPERVISOR_TOKEN", "a" * 40)
    monkeypatch.setattr(observer, "SupervisorLogReader", _CliReader)
    target = tmp_path / "baseline.json"
    assert observer.main(["baseline", "--baseline-file", str(target)]) == 0
    captured = capsys.readouterr()
    assert captured.out == "Baseline captured\n"
    assert "SECRET" not in captured.out + captured.err
    assert str(tmp_path) not in captured.out + captured.err
    assert target.stat().st_mode & 0o777 == 0o600
    saved = json.loads(target.read_text())
    assert saved["cursor"].startswith("s=SECRETMACHINE")
    # exclusive: a second capture never replaces an existing baseline
    assert observer.main(["baseline", "--baseline-file", str(target)]) == 1
    assert capsys.readouterr().err == "Bootstrap observer refused\n"


def test_baseline_cli_refuses_symlink_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A planted symlink is never followed when creating the baseline."""
    monkeypatch.setenv("SUPERVISOR_TOKEN", "a" * 40)
    monkeypatch.setattr(observer, "SupervisorLogReader", _CliReader)
    victim = tmp_path / "victim"
    link = tmp_path / "baseline.json"
    link.symlink_to(victim)
    assert observer.main(["baseline", "--baseline-file", str(link)]) == 1
    assert not victim.exists()
    capsys.readouterr()


_OBSERVED = observer.BootstrapObservation(
    observer.BootstrapPhase.UNKNOWN, False, True, False, True, True
)
_EMPTY_SHA = hashlib.sha256(b"").hexdigest()
_GOOD_BASELINE = json.dumps({"bytes": 0, "sha256": _EMPTY_SHA, "cursor": ""})


class _NoNetworkProbe:
    """Fail loudly if a CLI test ever uses the loopback API probe."""

    def ready(self, *_args: Any) -> bool:
        """Refuse any probe call."""
        raise AssertionError("CLI test must not reach the network")


@pytest.fixture
def offline_observe(monkeypatch: pytest.MonkeyPatch) -> list[observer.LogBaseline]:
    """Keep observer CLI tests offline."""
    seen: list[observer.LogBaseline] = []

    def fake_observe(
        _reader: Any, _probe: Any, loaded: observer.LogBaseline, _deadline: float, **_kw: Any
    ) -> observer.BootstrapObservation:
        seen.append(loaded)
        return _OBSERVED

    monkeypatch.setenv("SUPERVISOR_TOKEN", "a" * 40)
    monkeypatch.setattr(observer, "_watchdog_deadline", lambda *_a: time.monotonic() + 1000)
    monkeypatch.setattr(observer, "_lease_is_live", lambda *_a: True)
    monkeypatch.setattr(observer, "LoopbackApiProbe", _NoNetworkProbe)
    monkeypatch.setattr(observer, "observe", fake_observe)
    return seen


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "[]",
        '{"bytes":1}',
        '{"bytes":1,"sha256":"%s","cursor":"","x":1}' % ("0" * 64),
        '{"bytes":1,"sha256":"%s","cursor":"bad cursor"}' % ("0" * 64),
    ],
)
def test_observe_cli_refuses_bad_baseline_files(
    tmp_path: Path,
    offline_observe: list[observer.LogBaseline],
    capsys: pytest.CaptureFixture[str],
    payload: str,
) -> None:
    """Malformed, extra-key or injected-cursor baselines are refused with the fixed line."""
    path = tmp_path / "b.json"
    path.write_text(payload)
    path.chmod(0o600)
    assert observer.main(["observe", "--run", "r", "--baseline-file", str(path)]) == 1
    captured = capsys.readouterr()
    assert captured.err == "Bootstrap observer refused\n"
    assert captured.out == ""
    assert offline_observe == []


def test_observe_cli_accepts_a_valid_private_baseline(
    tmp_path: Path,
    offline_observe: list[observer.LogBaseline],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Positive control: a valid 0600 regular baseline is loaded and printed as safe JSON."""
    path = tmp_path / "b.json"
    path.write_text(_GOOD_BASELINE)
    path.chmod(0o600)
    assert observer.main(["observe", "--run", "r", "--baseline-file", str(path)]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == asdict(_OBSERVED) | {"phase": _OBSERVED.phase.value}
    assert captured.err == ""
    assert offline_observe == [observer.LogBaseline(0, _EMPTY_SHA, "")]


def test_observe_cli_refuses_loose_permissions_and_symlinks(
    tmp_path: Path,
    offline_observe: list[observer.LogBaseline],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A group-readable or symlinked baseline file is refused before any observation."""
    loose = tmp_path / "loose.json"
    loose.write_text(_GOOD_BASELINE)
    loose.chmod(0o644)
    real = tmp_path / "real.json"
    real.write_text(_GOOD_BASELINE)
    real.chmod(0o600)
    link = tmp_path / "link.json"
    link.symlink_to(real)
    for unsafe in (loose, link):
        assert observer.main(["observe", "--run", "r", "--baseline-file", str(unsafe)]) == 1
        captured = capsys.readouterr()
        assert captured.err == "Bootstrap observer refused\n"
        assert captured.out == ""
    assert offline_observe == []


def test_budget_exhaustion_after_lease_loss_is_unknown() -> None:
    """The final budget-exhausted result also requires a live lease."""
    log = current("start")
    seen = log + current(ANCHOR)
    clock = Clock()
    kwargs: dict[str, Any] = {"now": clock.now, "sleep": clock.sleep}
    live = observer.observe(
        SequenceReader([seen]), SequenceProbe([False]), baseline(log), 500.0, **kwargs
    )
    assert live.expected_source_seen
    assert not live.budget_remaining
    clock.value = 0.0
    # the lease is alive for every poll (budget ends at 380s) and dies only afterwards
    result = observer.observe(
        SequenceReader([seen]),
        SequenceProbe([False]),
        baseline(log),
        500.0,
        lease_live=lambda: clock.value < 380.0,
        **kwargs,
    )
    assert result.phase is observer.BootstrapPhase.UNKNOWN


# Canonical installer transcript for `server_repo: nredd/server@<sha>`: entrypoint echoes
# plus uv output (shapes only; no private values).
INSTALLER_LINES = (
    ANCHOR,
    f"Server repository: nredd/server@{SOURCE}",
    f"Server URL: git+https://github.com/nredd/server@{SOURCE}",
    f"Updating https://github.com/nredd/server ({SOURCE})",
    f"Updated https://github.com/nredd/server ({SOURCE})",
)


@pytest.mark.parametrize("line", INSTALLER_LINES[:-1])
def test_each_canonical_installer_line_anchors(line: str) -> None:
    """Every real installer line for the exact source is a valid anchor."""
    raw = current("noise", line, "more")
    assert observer._find_anchor(raw, 0) == len(b"noise\n")


@pytest.mark.parametrize("wrap", ["{}\r", "\x1b[32m{}\x1b[0m", "\x1b[1m\x1b[32m{}\x1b[0m\r"])
def test_terminal_colour_and_carriage_return_are_tolerated(wrap: str) -> None:
    """Colour codes and CRLF do not hide the installer line."""
    raw = current("noise", wrap.format(ANCHOR))
    assert observer._find_anchor(raw, 0) == len(b"noise\n")


def test_real_installer_transcript_reaches_api_ready() -> None:
    """A boot shaped like the real add-on log leaves `unknown` and probes the API."""
    old = current("old boot line")
    log = old + current(
        "",
        "Music Assistant Developer App",
        ANCHOR,
        "",
        "Step 1: Installing Music Assistant Server",
        f"Server repository: nredd/server@{SOURCE}",
        f"Server URL: git+https://github.com/nredd/server@{SOURCE}",
        "Installing dependencies from: https://raw.githubusercontent.com/x/requirements_all.txt",
        f"Updating https://github.com/nredd/server ({SOURCE})",
        f"Updated https://github.com/nredd/server ({SOURCE})",
        f"Built music-assistant @ git+https://github.com/nredd/server@{SOURCE}",
        STARTED,
    )
    clock = Clock()
    probe = SequenceProbe([True])
    result = observer.observe(
        SequenceReader([log]),
        probe,
        baseline(old),
        observer.CLEANUP_RESERVE + 100.0,
        now=clock.now,
        sleep=clock.sleep,
    )
    assert result.phase is observer.BootstrapPhase.API_READY
    assert result.expected_source_seen
    assert result.api_ready


OTHER_SHA = "0" * 40
NEAR_MISSES = [
    pytest.param(
        f"Building from source using server_repo: nredd/server@{OTHER_SHA}", id="other-sha"
    ),
    pytest.param(
        f"Building from source using server_repo: other/server@{SOURCE}", id="other-owner"
    ),
    pytest.param(
        f"Building from source using server_repo: nredd/serverx@{SOURCE}", id="other-repo"
    ),
    pytest.param(
        f"Building from source using server_repo: xnredd/server@{SOURCE}", id="owner-prefix"
    ),
    pytest.param(
        f"Building from source using server_repo: nredd/server@{SOURCE[:-1]}", id="sha-prefix"
    ),
    pytest.param(
        f"Building from source using server_repo: nredd/server@{SOURCE}0", id="sha-suffix"
    ),
    pytest.param(f"Building from source using server_repo: nredd/server@0{SOURCE}", id="sha-lead"),
    pytest.param(
        f"Building from source using server_repo: nredd/server@{SOURCE}-x", id="ref-suffix"
    ),
    pytest.param(
        f"Building from source using server_repo: nredd/server@{SOURCE.upper()}", id="case"
    ),
    pytest.param(f"xx {ANCHOR}", id="line-prefix"),
    pytest.param(f"{ANCHOR} xx", id="line-suffix"),
    pytest.param(f"echo {ANCHOR}", id="quoted-prefix"),
    pytest.param(f"Server repository: nredd/server@{SOURCE}x", id="repository-suffix"),
    pytest.param(
        f"Server URL: git+https://github.com/nredd/server@{OTHER_SHA}", id="url-other-sha"
    ),
    pytest.param(f"Server URL: git+https://github.com/other/server@{SOURCE}", id="url-other-owner"),
    pytest.param(f"Server URL: git+https://github.com/nredd/server@{SOURCE}.git", id="url-suffix"),
    pytest.param(f"Updating https://github.com/nredd/server ({OTHER_SHA})", id="uv-other-sha"),
    pytest.param(f"Updating https://github.com/other/server ({SOURCE})", id="uv-other-repo"),
    pytest.param(f"Updating https://github.com/nredd/server ({SOURCE[:-1]})", id="uv-sha-prefix"),
    pytest.param(f"Updating https://github.com/nredd/server ({SOURCE}0)", id="uv-sha-suffix"),
    pytest.param(f"Updating https://github.com/nredd/server ({SOURCE}) now", id="uv-line-suffix"),
    pytest.param(f"Updated https://github.com/nredd/server ({SOURCE})", id="uv-updated-alone"),
    pytest.param(f"Updating nredd/server@{SOURCE}", id="legacy-synthetic"),
    pytest.param(f"Installing nredd/server@{SOURCE}", id="legacy-synthetic-installing"),
    pytest.param(f"server_repo: nredd/server@{SOURCE}", id="selector-alone"),
    pytest.param(f"nredd/server@{SOURCE}", id="bare-selector"),
    pytest.param(
        f"Built music-assistant @ git+https://github.com/nredd/server@{SOURCE}", id="built"
    ),
    pytest.param(STARTED, id="startup-line-without-anchor"),
    pytest.param("Loaded player provider AirPlay", id="provider-alone"),
]


RUNTIME_SHA = "3ff62b9cf15a93a49aeec9789a871d1112faa15f"
REPLACED_RUNTIME_SHA = "208e9c069163a1c9e1a4ed6da16015d69d972d34"
SUPERSEDED_RUNTIME_SHA = "4799bd57c86dff1bb39a7333c2a5e9bf91f8812b"
PRIOR_RUNTIME_SHA = "dae8ae9484cd19d847bf495bb95a85e6d1afc2c2"
LAST_RUNTIME_SHA = "ad73c02283350952f535e328f7c001c8b209e9fa"
PREVIOUS_RUNTIME_SHA = "a6382b99fad1603176fe89daf0a233406e77f5ad"
OLDER_RUNTIME_SHA = "0f7b543127f71c5639d55d4e0e34168ba92d0c33"


def test_runtime_pin_is_the_reviewed_merge() -> None:
    """The pin is the literal reviewed runtime merge, not a symbolic self-reference."""
    assert observer.EXPECTED_SOURCE == RUNTIME_SHA


@pytest.mark.parametrize(
    "template",
    [
        "Building from source using server_repo: nredd/server@{}",
        "Server repository: nredd/server@{}",
        "Server URL: git+https://github.com/nredd/server@{}",
        "Updating https://github.com/nredd/server ({})",
    ],
)
def test_real_runtime_sha_anchors_and_previous_sha_does_not(template: str) -> None:
    """Each canonical line anchors only for the literal runtime SHA, never the old pin."""
    old = current("old boot line")
    good = old + current(template.format(RUNTIME_SHA))
    assert observer._find_anchor(good, len(old)) == len(old)
    for stale_sha in (
        REPLACED_RUNTIME_SHA,
        SUPERSEDED_RUNTIME_SHA,
        PRIOR_RUNTIME_SHA,
        LAST_RUNTIME_SHA,
        PREVIOUS_RUNTIME_SHA,
        OLDER_RUNTIME_SHA,
    ):
        stale = old + current(template.format(stale_sha))
        assert observer._find_anchor(stale, len(old)) is None


@pytest.mark.parametrize("line", NEAR_MISSES)
def test_near_misses_never_anchor(line: str) -> None:
    """Other repos, SHAs, prefix or suffix collisions and generic lines stay `unknown`."""
    old = current("old boot line")
    log = old + current(line, STARTED, "aiolibdatachannel git update")
    assert observer._find_anchor(log, len(old)) is None
    clock = Clock()
    probe = SequenceProbe([True, True])
    result = observer.observe(
        SequenceReader([log]),
        probe,
        baseline(old),
        observer.CLEANUP_RESERVE + 10.0,
        now=clock.now,
        sleep=clock.sleep,
    )
    assert result.phase is observer.BootstrapPhase.UNKNOWN
    assert not result.expected_source_seen
    assert not result.api_ready
    assert probe.deadlines == []


def test_stale_baseline_anchor_is_not_a_current_anchor() -> None:
    """The exact installer line in the pre-start baseline does not anchor the new boot."""
    old = current(*INSTALLER_LINES, STARTED)
    log = old + current(STARTED)
    assert observer._find_anchor(old, 0) is not None
    assert observer._find_anchor(log, len(old)) is None


@pytest.mark.parametrize(
    "line",
    [
        "Step 1: Installing Music Assistant Server",
        "Downloading music-assistant-server",
        "Building music-assistant @ file:///x",
        "Music Assistant Server docs: listening on port 8095 in pip",
        "Starting Music Assistant Server",
        "Starting Music Assistant",
        "uvicorn listening on 0.0.0.0",
    ],
)
def test_pre_runtime_lines_are_not_runtime_and_never_probe(line: str) -> None:
    """Install-step and product-name lines after a valid anchor stay below `runtime`."""
    old = current("old boot line")
    log = old + current(ANCHOR, line)
    clock = Clock()
    probe = SequenceProbe([True])
    result = observer.observe(
        SequenceReader([log]),
        probe,
        baseline(old),
        observer.CLEANUP_RESERVE + 10.0,
        now=clock.now,
        sleep=clock.sleep,
    )
    assert result.phase is not observer.BootstrapPhase.RUNTIME
    assert result.phase is not observer.BootstrapPhase.API_READY
    assert not result.api_ready
    assert probe.deadlines == []


def test_truncated_real_transcript_before_built_is_not_api_ready() -> None:
    """A transcript cut off after `Step 1` must not probe or report ready."""
    old = current("old boot line")
    log = old + current(
        ANCHOR,
        "",
        "Step 1: Installing Music Assistant Server",
        f"Server repository: nredd/server@{SOURCE}",
    )
    clock = Clock()
    probe = SequenceProbe([True])
    result = observer.observe(
        SequenceReader([log]),
        probe,
        baseline(old),
        observer.CLEANUP_RESERVE + 10.0,
        now=clock.now,
        sleep=clock.sleep,
    )
    assert not result.api_ready
    assert result.phase is observer.BootstrapPhase.SOURCE_INSTALL
    assert probe.deadlines == []
