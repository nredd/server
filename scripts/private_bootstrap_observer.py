"""
Observe one bounded private DEV bootstrap without lifecycle mutation authority.

References:
- https://contributors.home-assistant.io/apps/supervisor-api/apps/

The observer is intentionally separate from `private_dev_lifecycle.py`: it may read a
fixed add-on log endpoint and the private loopback `/info`, but cannot start, stop, or
change DEV options. It emits finite classifications only, never log text, tokens, paths,
or identities.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from scripts import private_dev_lifecycle as lifecycle

EXPECTED_SOURCE = "0f7b543127f71c5639d55d4e0e34168ba92d0c33"
DEV = lifecycle.DEV
PRIVATE_INFO_URL = "http://127.0.0.1:18095/info"
MAX_LOG_BYTES = 256 * 1024
MAX_API_BYTES = 4096
HTTP_TIMEOUT = 3.0
STARTUP_BUDGET = 480.0
CLEANUP_RESERVE = 120.0
POLL_INTERVAL = 2.0

_INSTALLER_ANCHOR = re.compile(
    rb"\b(?:installing|updating)\b.*\bnredd/server@" + EXPECTED_SOURCE.encode() + rb"\b",
    re.IGNORECASE,
)
_DEPENDENCY_FETCH = re.compile(
    r"\b(?:aiolibdatachannel|git update|resolving|fetching)\b", re.IGNORECASE
)
_SOURCE_INSTALLED = re.compile(
    r"\b(?:installed|built)\b.*\b(?:music-assistant|server)\b", re.IGNORECASE
)
_RUNTIME_STARTED = re.compile(
    r"\b(?:starting music assistant|music assistant server|listening on)\b", re.IGNORECASE
)
_CURRENT_FAILURE = re.compile(
    r"\b(?:traceback|trialbinaryerror|jsondecodeerror|installation failed)\b", re.IGNORECASE
)


class BootstrapPhase(StrEnum):
    """Finite current-boot bootstrap classifications."""

    FETCHING_DEPS = "fetching-deps"
    SOURCE_INSTALL = "source-install"
    RUNTIME = "runtime"
    API_READY = "api-ready"
    FAILURE = "failure"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class LogBaseline:
    """A safe pre-start boundary for one log stream."""

    bytes: int
    sha256: str


@dataclass(frozen=True)
class BootstrapObservation:
    """Safe bounded result for a single observer invocation."""

    phase: BootstrapPhase
    expected_source_seen: bool
    current_boot: bool
    api_ready: bool
    budget_remaining: bool
    log_continuity: bool


class LogReader(Protocol):
    """Read the bounded current add-on log stream without exposing it to callers."""

    def read(self, deadline: float) -> bytes:
        """Return current raw bytes, bounded by this module's fixed response limit."""


class ApiProbe(Protocol):
    """Probe only the fixed unauthenticated loopback `/info` endpoint."""

    def ready(self, deadline: float) -> bool:
        """Return whether a bounded valid `/info` response was observed."""


class ObserverError(RuntimeError):
    """A sanitized observer failure which must not be treated as a bootstrap result."""


class SupervisorLogReader:
    """Read the one fixed DEV log endpoint through native curl with no output leak."""

    def __init__(self, token: str) -> None:
        """Keep the Supervisor token private to the in-memory curl configuration."""
        self._token = lifecycle.validate_token(token)

    def read(self, deadline: float) -> bytes:
        """Fetch bounded current log bytes with no redirect or inherited environment."""
        remaining = _remaining(deadline)
        url = f"{lifecycle.SUPERVISOR_BASE_URL}/addons/{DEV}/logs"
        config = (
            "\n".join(
                (
                    'header = "Accept: text/plain"',
                    f'url = "{_curl_value(url)}"',
                    f"max-time = {remaining:.6f}",
                    f"max-filesize = {MAX_LOG_BYTES}",
                    f'header = "Authorization: {_curl_value(f"Bearer {self._token}")}"',
                )
            ).encode()
            + b"\n"
        )
        try:
            result = subprocess.run(
                ["/usr/bin/curl", "--disable", "--config", "-"],
                input=config,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=remaining,
                check=False,
                env={},
            )
        except (OSError, subprocess.TimeoutExpired) as err:
            raise ObserverError("log capture unavailable") from err
        if result.returncode == 63:
            raise ObserverError("log capture exceeded limit")
        if result.returncode != 0 or len(result.stdout) > MAX_LOG_BYTES:
            raise ObserverError("log capture unavailable")
        return result.stdout


class LoopbackApiProbe:
    """Probe the fixed private API without credentials, redirects, or response disclosure."""

    def ready(self, deadline: float) -> bool:
        """Return true only for a bounded JSON object from the fixed loopback endpoint."""
        try:
            remaining = min(HTTP_TIMEOUT, _remaining(deadline))
            cfg = (
                "\n".join(
                    (
                        f'url = "{_curl_value(PRIVATE_INFO_URL)}"',
                        f"max-time = {remaining:.6f}",
                        f"max-filesize = {MAX_API_BYTES}",
                        'write-out = "\\n%{http_code}"',
                    )
                ).encode()
                + b"\n"
            )
            result = subprocess.run(
                ["/usr/bin/curl", "--disable", "--config", "-"],
                input=cfg,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=remaining,
                check=False,
                env={},
            )
        except (OSError, subprocess.TimeoutExpired, ObserverError):
            return False
        if result.returncode != 0:
            return False
        body, separator, status = result.stdout.rpartition(b"\n")
        if not separator or status != b"200" or len(body) > MAX_API_BYTES:
            return False
        try:
            return isinstance(json.loads(body), dict) and _remaining(deadline) > 0
        except (UnicodeDecodeError, json.JSONDecodeError, ObserverError):
            return False


def capture_baseline(reader: LogReader, deadline: float) -> LogBaseline:
    """Capture a safe pre-start log boundary without persisting or printing log content."""
    raw = reader.read(deadline)
    return LogBaseline(len(raw), hashlib.sha256(raw).hexdigest())


def observe(
    reader: LogReader,
    probe: ApiProbe,
    baseline: LogBaseline,
    watchdog_deadline: float,
    *,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> BootstrapObservation:
    """
    Observe only the current boot inside the watchdog's fixed cleanup reserve.

    :param reader: Bounded current log reader.
    :param probe: Fixed loopback API probe.
    :param baseline: Pre-start log identity and length.
    :param watchdog_deadline: Monotonic deadline inherited from the armed lifecycle lease.
    :param now: Monotonic clock seam for tests.
    :param sleep: Bounded sleep seam for tests.
    """
    _validate_baseline(baseline)
    started = now()
    deadline = min(watchdog_deadline - CLEANUP_RESERVE, started + STARTUP_BUDGET)
    if deadline <= started:
        return _unknown(False)
    previous: bytes | None = None
    anchor_offset: int | None = None
    last_phase = BootstrapPhase.UNKNOWN
    while now() < deadline:
        try:
            raw = reader.read(deadline)
        except ObserverError:
            return _unknown(False)
        if now() >= deadline:
            return _unknown(False)
        if previous is not None and (len(raw) < len(previous) or not raw.startswith(previous)):
            return _unknown(False)
        previous = raw
        if (
            len(raw) < baseline.bytes
            or hashlib.sha256(raw[: baseline.bytes]).hexdigest() != baseline.sha256
        ):
            return _unknown(False)
        if anchor_offset is None:
            anchor = _find_anchor(raw, baseline.bytes)
            if anchor is not None:
                anchor_offset = anchor
        if anchor_offset is not None:
            phase = _classify(raw[anchor_offset:])
            if phase is BootstrapPhase.FAILURE:
                return BootstrapObservation(
                    phase=phase,
                    expected_source_seen=True,
                    current_boot=True,
                    api_ready=False,
                    budget_remaining=True,
                    log_continuity=True,
                )
            if phase is BootstrapPhase.RUNTIME and probe.ready(deadline):
                return BootstrapObservation(
                    phase=BootstrapPhase.API_READY,
                    expected_source_seen=True,
                    current_boot=True,
                    api_ready=True,
                    budget_remaining=True,
                    log_continuity=True,
                )
            if now() >= deadline:
                return _unknown(False)
            last_phase = phase
        sleep(min(POLL_INTERVAL, max(0.0, deadline - now())))
    if anchor_offset is not None:
        return BootstrapObservation(
            phase=last_phase,
            expected_source_seen=True,
            current_boot=True,
            api_ready=False,
            budget_remaining=False,
            log_continuity=True,
        )
    return _unknown(False)


def main(argv: list[str] | None = None) -> int:
    """Capture a baseline or emit one safe bounded bootstrap observation as JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("baseline", "observe"))
    parser.add_argument("--root", type=Path, default=lifecycle.ROOT)
    parser.add_argument("--run")
    parser.add_argument("--baseline")
    args = parser.parse_args(argv)
    try:
        token = lifecycle.validate_token(os.environ.get("SUPERVISOR_TOKEN", ""))
        reader = SupervisorLogReader(token)
        if args.command == "baseline":
            baseline = capture_baseline(reader, time.monotonic() + HTTP_TIMEOUT)
            sys.stdout.write(json.dumps(asdict(baseline), sort_keys=True) + "\n")
            return 0
        if args.run is None or args.baseline is None:
            parser.error("observe requires --run and --baseline")
        baseline = LogBaseline(**json.loads(args.baseline))
        result = observe(
            reader, LoopbackApiProbe(), baseline, _watchdog_deadline(args.root, args.run)
        )
        sys.stdout.write(json.dumps(asdict(result), sort_keys=True) + "\n")
        return 0
    except (lifecycle.LifecycleError, ObserverError, TypeError, ValueError, json.JSONDecodeError):
        sys.stderr.write("Bootstrap observer refused\n")
        return 1


def _classify(current_boot: bytes) -> BootstrapPhase:
    """Classify only evidence strictly after the new exact-source installer anchor."""
    text = current_boot.decode("utf-8", "replace")
    if _CURRENT_FAILURE.search(text):
        return BootstrapPhase.FAILURE
    if _RUNTIME_STARTED.search(text):
        return BootstrapPhase.RUNTIME
    if _SOURCE_INSTALLED.search(text) or _DEPENDENCY_FETCH.search(text):
        return BootstrapPhase.FETCHING_DEPS
    return BootstrapPhase.SOURCE_INSTALL


def _find_anchor(raw: bytes, offset: int) -> int | None:
    """Return the first exact expected-source installer anchor after the baseline."""
    match = _INSTALLER_ANCHOR.search(raw, offset)
    return match.start() if match else None


def _unknown(current_boot: bool) -> BootstrapObservation:
    """Return the fail-closed classification for absent or untrustworthy evidence."""
    return BootstrapObservation(
        phase=BootstrapPhase.UNKNOWN,
        expected_source_seen=False,
        current_boot=current_boot,
        api_ready=False,
        budget_remaining=False,
        log_continuity=False,
    )


def _remaining(deadline: float) -> float:
    """Return usable absolute-deadline time, refusing non-finite or exhausted budgets."""
    remaining = deadline - time.monotonic()
    if not math.isfinite(remaining) or remaining <= 0:
        raise ObserverError("observer deadline expired")
    return remaining


def _validate_baseline(baseline: LogBaseline) -> None:
    """Require exactly one safe bounded pre-start prefix identity."""
    if (
        not isinstance(baseline.bytes, int)
        or baseline.bytes < 0
        or baseline.bytes > MAX_LOG_BYTES
        or not re.fullmatch(r"[0-9a-f]{64}", baseline.sha256)
    ):
        raise ObserverError("log baseline is invalid")


def _curl_value(value: str) -> str:
    """Escape a curl configuration value without accepting controls."""
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ObserverError("HTTP request value is invalid")
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _watchdog_deadline(root: Path, run_id: str) -> float:
    """Read only a live armed lease deadline; this observer never arms or mutates it."""
    run_dir, snapshot = lifecycle.load_snapshot(root, run_id)
    watch = lifecycle._load_watchdog(run_dir, snapshot)
    _require_live_lease(root, snapshot.run_id, snapshot.nonce)
    deadline = watch.get("monotonic_deadline")
    if not isinstance(deadline, float) or not math.isfinite(deadline):
        raise ObserverError("watchdog lease deadline is invalid")
    if deadline - time.monotonic() <= CLEANUP_RESERVE:
        raise ObserverError("watchdog lease has no cleanup reserve")
    return deadline


def _require_live_lease(root: Path, run_id: str, nonce: str) -> None:
    """Require the existing watchdog lease to be live and owned by this run without creating it."""
    try:
        descriptor = os.open(root / "watchdog.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as err:
        raise ObserverError("watchdog lease is unavailable") from err
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ObserverError("watchdog lease is unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            raise ObserverError("watchdog lease is not live")
        if info.st_size > MAX_LOG_BYTES:
            raise ObserverError("watchdog lease is malformed")
        try:
            owner = json.loads(os.pread(descriptor, MAX_LOG_BYTES, 0))
        except (OSError, ValueError) as err:
            raise ObserverError("watchdog lease is malformed") from err
        if owner != {"run_id": run_id, "nonce": nonce}:
            raise ObserverError("watchdog lease belongs to another run")
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
