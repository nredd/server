"""Offline tests for the bounded private DEV bootstrap observer."""

from __future__ import annotations

import hashlib
import json
import os
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
    assert observer.main(["observe", "--run", "token-like-run", "--baseline", "{}"]) == 1
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
    """A torn-down owner run must not keep yielding bootstrap results."""
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


def test_bool_baseline_size_is_refused() -> None:
    """`True` is an int subclass but not a byte length."""
    with pytest.raises(observer.ObserverError, match="baseline"):
        observer.observe(
            SequenceReader([b""]),
            SequenceProbe([False]),
            observer.LogBaseline(True, "0" * 64),  # type: ignore[arg-type]
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

    monkeypatch.setattr(observer.subprocess, "run", run)
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
    monkeypatch.setattr(observer.subprocess, "run", lambda *_a, **_k: _Completed(0, stdout))
    assert not observer.LoopbackApiProbe().ready(time.monotonic() + 5)


def test_api_probe_accepts_json_object(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bounded JSON object with 200 is ready."""
    monkeypatch.setattr(
        observer.subprocess, "run", lambda *_a, **_k: _Completed(0, b'{"a":1}\n200')
    )
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
