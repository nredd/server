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
        f"Updating nredd/server@{SOURCE}",
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
        f"Updating nredd/server@{SOURCE}",
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
    install = old + current(f"Updating nredd/server@{SOURCE}", "aiolibdatachannel git update begun")
    runtime = install + current("Installed music-assistant", "Starting Music Assistant")
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
    old = current(f"Updating nredd/server@{SOURCE}", "Starting Music Assistant")
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
    log = old + current(f"Updating nredd/server@{SOURCE}", "fetching dependencies")
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
    log = old + current(f"Updating nredd/server@{SOURCE}", "Traceback TrialBinaryError: rejected")
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
    anchored = old + current(f"Updating nredd/server@{SOURCE}")
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
    log = old + current(f"Updating nredd/server@{SOURCE}", "fetching dependencies")
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
    log = old + current(f"Updating nredd/server@{SOURCE}", "fetching dependencies")
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
    log = old + current(f"Updating nredd/server@{SOURCE}", "fetching dependencies")
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
    reader = SequenceReader([log, log + current(f"Installing nredd/server@{SOURCE}")])
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
    def __init__(self, returncode: int, stdout: bytes) -> None:
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

    def ready(self, _deadline: float) -> bool:
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
    runtime = log + current(f"Installing nredd/server@{SOURCE}", "Starting Music Assistant")
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
    journal.append(f"Installing nredd/server@{SOURCE}")
    journal.append(*[f"Fetching wheel {i}" for i in range(400)])
    journal.append("Starting Music Assistant")
    reader = observer.SupervisorLogReader(token, base.cursor)
    result = observer.observe(
        reader,
        type("P", (), {"ready": lambda _s, _d: True})(),
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
    journal.append(f"Installing nredd/server@{SOURCE}")
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
    bad = log + current(f"Installing nredd/server@{SOURCE}", "Traceback boom")
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
        self.first_cursor: str | None = "s=SECRETMACHINE;i=1;b=SECRETBOOT"

    def read(self, _deadline: float) -> bytes:
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


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "[]",
        '{"bytes":1}',
        '{"bytes":1,"sha256":"%s","cursor":"bad cursor","x":1}' % ("0" * 64),
        '{"bytes":1,"sha256":"%s","cursor":"bad cursor"}' % ("0" * 64),
    ],
)
def test_observe_cli_refuses_bad_baseline_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: str,
) -> None:
    """Malformed, extra-key or injected-cursor baselines are refused with the fixed line."""
    monkeypatch.setenv("SUPERVISOR_TOKEN", "a" * 40)
    monkeypatch.setattr(observer, "_watchdog_deadline", lambda *_a: time.monotonic() + 1000)
    path = tmp_path / "b.json"
    path.write_text(payload)
    path.chmod(0o600)
    assert observer.main(["observe", "--run", "r", "--baseline-file", str(path)]) == 1
    captured = capsys.readouterr()
    assert captured.err == "Bootstrap observer refused\n"
    assert captured.out == ""


def test_observe_cli_refuses_loose_permissions_and_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A group-readable or symlinked baseline file is unsafe input."""
    monkeypatch.setenv("SUPERVISOR_TOKEN", "a" * 40)
    good = json.dumps({"bytes": 0, "sha256": hashlib.sha256(b"").hexdigest(), "cursor": ""})
    loose = tmp_path / "loose.json"
    loose.write_text(good)
    loose.chmod(0o644)
    assert observer.main(["observe", "--run", "r", "--baseline-file", str(loose)]) == 1
    link = tmp_path / "link.json"
    real = tmp_path / "real.json"
    real.write_text(good)
    real.chmod(0o600)
    link.symlink_to(real)
    assert observer.main(["observe", "--run", "r", "--baseline-file", str(link)]) == 1
    assert capsys.readouterr().out == ""
