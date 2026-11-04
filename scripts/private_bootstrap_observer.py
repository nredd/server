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

EXPECTED_SOURCE = "dae8ae9484cd19d847bf495bb95a85e6d1afc2c2"
DEV = lifecycle.DEV
PRIVATE_INFO_URL = "http://127.0.0.1:18095/info"
MAX_LOG_BYTES = 256 * 1024
MAX_HEADER_BYTES = 8192
MAX_LOG_ENTRIES = 2000
MAX_API_BYTES = 4096
HTTP_TIMEOUT = 3.0
STARTUP_BUDGET = 480.0
CLEANUP_RESERVE = 120.0
POLL_INTERVAL = 2.0

_CURSOR = re.compile(r"[A-Za-z0-9=;_.-]{1,512}")

# Real installer lines, from `music_assistant_dev/entrypoint.sh` in
# https://github.com/music-assistant/home-assistant-addon (sha256 125c6500...40f2) and uv.
# Each must be an entire line naming exactly `nredd/server` at `EXPECTED_SOURCE`, so another
# repo, another SHA, or any prefix or suffix of either cannot anchor a boot. Only terminal
# colour codes and a trailing carriage return may surround a line.
_SHA = re.escape(EXPECTED_SOURCE.encode())
_SGR = rb"(?:\x1b\[[0-9;]*m)*"
_INSTALLER_ANCHOR = re.compile(
    rb"^" + _SGR + rb"(?:"
    rb"Building from source using server_repo: nredd/server@" + _SHA + rb"|"
    rb"Server repository: nredd/server@" + _SHA + rb"|"
    rb"Server URL: git\+https://github\.com/nredd/server@" + _SHA + rb"|"
    rb"Updating https://github\.com/nredd/server \(" + _SHA + rb"\)"
    rb")" + _SGR + rb"\r?$",
    re.MULTILINE,
)
_DEPENDENCY_FETCH = re.compile(
    r"\b(?:aiolibdatachannel|git update|resolving|fetching)\b", re.IGNORECASE
)
_SOURCE_INSTALLED = re.compile(
    r"\b(?:installed|built)\b.*\b(?:music-assistant|server)\b", re.IGNORECASE
)
# `music_assistant/mass.py` logs this once, after the install step, as
# `Starting Music Assistant Server (<id>) version <v> - HA add-on: ...`. The entrypoint's
# `Step 1: Installing Music Assistant Server`, dependency logs and product mentions never match.
_RUNTIME_STARTED = re.compile(r"\bStarting Music Assistant Server \([^)\n]*\) version \S")
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
    """
    A safe pre-start boundary for one log stream.

    The cursor identifies the first baseline entry (empty for an empty stream); it holds
    machine and boot identifiers, so keep it out of output.
    """

    bytes: int
    sha256: str
    cursor: str = ""


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

    first_cursor: str | None

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

    def __init__(self, token: str, cursor: str | None = None) -> None:
        """
        Keep the Supervisor token private to the in-memory curl configuration.

        :param token: Supervisor token.
        :param cursor: Baseline journal cursor to anchor on, or None to read the last two
            entries (used only to capture a baseline). An empty cursor reads from the start.
        """
        self._token = lifecycle.validate_token(token)
        if cursor is not None and cursor != "" and not _CURSOR.fullmatch(cursor):
            raise ObserverError("log cursor is invalid")
        self._cursor = cursor
        self.first_cursor: str | None = None

    def read(self, deadline: float) -> bytes:
        """
        Fetch bounded log bytes.

        :param deadline: Absolute `time.monotonic()` deadline for the request.
        :return: Raw log bytes: the last two entries when no cursor was given, otherwise
            every entry from the baseline cursor onward.
        :raises ObserverError: On an expired deadline, curl or HTTP failure, an oversized
            or entry-capped body, an invalid cursor, or a baseline cursor no longer present.
        """
        remaining = _remaining(deadline)
        # Supervisor maps the Range header to journal-gatewayd `entries=cursor[:skip]:count`;
        # the default is a sliding tail of the last 100 entries.
        if self._cursor is None:
            entries = "entries=:-1:2"
        else:
            entries = f"entries={self._cursor}:0:{MAX_LOG_ENTRIES}"
        url = f"{lifecycle.SUPERVISOR_BASE_URL}/addons/{DEV}/logs"
        cfg = (
            "\n".join(
                (
                    "fail",
                    'dump-header = "-"',
                    f'header = "Range: {entries}"',
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
                input=cfg,
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
        if result.returncode != 0:
            raise ObserverError("log capture unavailable")
        head, separator, body = result.stdout.partition(b"\r\n\r\n")
        if not separator or len(head) > MAX_HEADER_BYTES or len(body) > MAX_LOG_BYTES:
            raise ObserverError("log capture unavailable")
        if self._cursor is not None and body.count(b"\n") >= MAX_LOG_ENTRIES:
            # gatewayd silently truncates at the entry count; an entry has at least one line.
            raise ObserverError("log capture exceeded entry limit")
        self.first_cursor = _first_cursor(head)
        if self._cursor is not None and self._cursor != "":
            # Anchored reads must start at the baseline entry; otherwise it was rotated away.
            if self.first_cursor != self._cursor:
                raise ObserverError("log baseline cursor is no longer present")
        elif body and self.first_cursor is None:
            raise ObserverError("log capture has no cursor")
        return body


class LoopbackApiProbe:
    """Probe the fixed private API without credentials, redirects, or response disclosure."""

    def ready(self, deadline: float) -> bool:
        """
        Probe the fixed loopback `/info` endpoint.

        :param deadline: Absolute `time.monotonic()` deadline for the request.
        :return: True only for a bounded JSON object with HTTP 200 before the deadline.
        """
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
    """
    Capture a safe pre-start log boundary without persisting or printing log content.

    :param reader: Log reader positioned on the last two entries.
    :param deadline: Absolute `time.monotonic()` deadline for the read.
    :return: Length, SHA-256 and journal cursor of the captured boundary.
    :raises ObserverError: If the read fails or returned bytes without a cursor.
    """
    raw = reader.read(deadline)
    cursor = reader.first_cursor or ""
    if raw and not cursor:
        raise ObserverError("log capture has no cursor")
    return LogBaseline(len(raw), hashlib.sha256(raw).hexdigest(), cursor)


def observe(
    reader: LogReader,
    probe: ApiProbe,
    baseline: LogBaseline,
    watchdog_deadline: float,
    *,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    lease_live: Callable[[], bool] = lambda: True,
) -> BootstrapObservation:
    """
    Observe only the current boot inside the watchdog's fixed cleanup reserve.

    :param reader: Bounded current log reader.
    :param probe: Fixed loopback API probe.
    :param baseline: Pre-start log identity and length.
    :param watchdog_deadline: Monotonic deadline inherited from the armed lifecycle lease.
    :param now: Monotonic clock seam for tests.
    :param sleep: Bounded sleep seam for tests.
    :param lease_live: Called every poll, after a blocking API probe and before a failure
        result; a false result means the owner tore the run down.
    :return: A finite observation; `unknown` whenever continuity, lease or budget is lost.
    :raises ObserverError: If the baseline is invalid.
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
        if not lease_live():
            return _unknown(False)
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
                if now() >= deadline or not lease_live():
                    return _unknown(False)
                return BootstrapObservation(
                    phase=phase,
                    expected_source_seen=True,
                    current_boot=True,
                    api_ready=False,
                    budget_remaining=True,
                    log_continuity=True,
                )
            if phase is BootstrapPhase.RUNTIME and probe.ready(deadline):
                # The probe can block: the lease or budget may have ended meanwhile.
                if now() >= deadline or not lease_live():
                    return _unknown(False)
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
    if anchor_offset is not None and lease_live():
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
    """
    Capture a baseline file or emit one safe bounded bootstrap observation as JSON.

    :param argv: Command-line arguments; defaults to `sys.argv`.
    :return: Zero on success, one after a refusal (a fixed line is written to stderr).
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("baseline", "observe"))
    parser.add_argument("--root", type=Path, default=lifecycle.ROOT)
    parser.add_argument("--run")
    parser.add_argument("--baseline-file", type=Path)
    args = parser.parse_args(argv)
    try:
        token = lifecycle.validate_token(os.environ.get("SUPERVISOR_TOKEN", ""))
        if args.command == "baseline":
            if args.baseline_file is None:
                parser.error("baseline requires --baseline-file")
            baseline = capture_baseline(SupervisorLogReader(token), time.monotonic() + HTTP_TIMEOUT)
            # The cursor embeds machine and boot identifiers: keep it in an owner-only file.
            lifecycle._write_json_exclusive(args.baseline_file, asdict(baseline))
            sys.stdout.write("Baseline captured\n")
            return 0
        if args.run is None or args.baseline_file is None:
            parser.error("observe requires --run and --baseline-file")
        baseline = LogBaseline(**lifecycle._read_private_json(args.baseline_file, "baseline"))
        deadline = _watchdog_deadline(args.root, args.run)
        result = observe(
            SupervisorLogReader(token, baseline.cursor),
            LoopbackApiProbe(),
            baseline,
            deadline,
            lease_live=lambda: _lease_is_live(args.root, args.run),
        )
        sys.stdout.write(json.dumps(asdict(result), sort_keys=True) + "\n")
        return 0
    except (lifecycle.LifecycleError, ObserverError, OSError, KeyError, TypeError, ValueError):
        sys.stderr.write("Bootstrap observer refused\n")
        return 1


def _classify(current_boot: bytes) -> BootstrapPhase:
    """Classify only evidence strictly after the new exact-source installer anchor."""
    text = current_boot.decode("utf-8", "replace")
    if _CURRENT_FAILURE.search(text):
        return BootstrapPhase.FAILURE
    if _RUNTIME_STARTED.search(text):
        return BootstrapPhase.RUNTIME
    if _SOURCE_INSTALLED.search(text):
        return BootstrapPhase.SOURCE_INSTALL
    if _DEPENDENCY_FETCH.search(text):
        return BootstrapPhase.FETCHING_DEPS
    return BootstrapPhase.SOURCE_INSTALL


def _find_anchor(raw: bytes, offset: int) -> int | None:
    """Return the offset of the first full installer line for `EXPECTED_SOURCE` after the baseline."""
    match = _INSTALLER_ANCHOR.search(raw, offset)
    return match.start() if match else None


def _first_cursor(head: bytes) -> str | None:
    """Return the validated `X-First-Cursor` response header, if any."""
    for line in head.split(b"\r\n"):
        name, separator, value = line.partition(b":")
        if separator and name.strip().lower() == b"x-first-cursor":
            cursor = value.strip().decode("ascii", "replace")
            if not _CURSOR.fullmatch(cursor):
                raise ObserverError("log cursor is invalid")
            return cursor
    return None


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
    """
    Return the time left before an absolute monotonic deadline.

    :param deadline: Absolute `time.monotonic()` deadline.
    :return: Positive finite seconds remaining.
    :raises ObserverError: If the deadline is non-finite or already expired.
    """
    remaining = deadline - time.monotonic()
    if not math.isfinite(remaining) or remaining <= 0:
        raise ObserverError("observer deadline expired")
    return remaining


def _validate_baseline(baseline: LogBaseline) -> None:
    """Require exactly one safe bounded pre-start prefix identity."""
    if (
        type(baseline.bytes) is not int
        or baseline.bytes < 0
        or baseline.bytes > MAX_LOG_BYTES
        or not re.fullmatch(r"[0-9a-f]{64}", baseline.sha256)
        or type(baseline.cursor) is not str
        or (baseline.cursor != "" and not _CURSOR.fullmatch(baseline.cursor))
    ):
        raise ObserverError("log baseline is invalid")


def _curl_value(value: str) -> str:
    """Escape a curl configuration value without accepting controls."""
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ObserverError("HTTP request value is invalid")
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _watchdog_deadline(root: Path, run_id: str) -> float:
    """Read only a live armed lease deadline; this observer never arms or mutates it."""
    _, snapshot = lifecycle.load_snapshot(root, run_id)
    deadline = lifecycle.load_watchdog_deadline(root, run_id)
    _require_live_lease(root, snapshot.run_id, snapshot.nonce)
    if not math.isfinite(deadline):
        raise ObserverError("watchdog lease deadline is invalid")
    if deadline - time.monotonic() <= CLEANUP_RESERVE:
        raise ObserverError("watchdog lease has no cleanup reserve")
    return deadline


def _lease_is_live(root: Path, run_id: str) -> bool:
    """Return whether the run's watchdog lease is still live and owned by it."""
    try:
        _, snapshot = lifecycle.load_snapshot(root, run_id)
        _require_live_lease(root, snapshot.run_id, snapshot.nonce)
    except (lifecycle.LifecycleError, ObserverError, OSError, KeyError):
        return False
    return True


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
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
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
