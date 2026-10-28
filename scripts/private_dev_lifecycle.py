"""
Private, fail-closed DEV-only lifecycle controller for the stock-absent native AirPlay trial.

References:
- https://contributors.home-assistant.io/apps/supervisor-api/apps/

This file is deliberately private trial tooling for the state in which the stock Music
Assistant app has been REMOVED on purpose. It replaces, and never calls, the historical
`private_airplay_recovery.py` controller, which assumed stock installed and restored HA
groups; that controller and its receipts stay untouched as historical evidence.

The only mutations this controller can issue are `POST /addons/<DEV>/start` and
`POST /addons/<DEV>/stop`. Its fixed path allowlist contains no stock app path, no store
path and no Core API path, so it cannot install or start stock, reload an HA entry, group,
play, or change volume on ANY player. Office exclusion is therefore structural here, not
a runtime check. It runs on the SSH app's Python 3.13 with the stdlib and native
`/usr/bin/curl` only.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import math
import os
import re
import secrets
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

ROOT = Path("/data/airplay-native-probe/dev-lifecycle")
STOCK = "d5369777_music_assistant"
DEV = "d5369777_music_assistant_dev"
MAX_RESPONSE = 256 * 1024
RESTORE_TIMEOUT = 90.0
SCHEMA = 2
MAX_RUN_AGE = 600.0
MAX_WATCHDOG_TIMEOUT = 3600.0
LOCK_TIMEOUT = 600.0
STOP_POLL_INTERVAL = 1.0
SUPERVISOR_BASE_URL = "http://supervisor"
PATH_PATTERN = re.compile(rf"/addons(?:/{DEV}/(?:info|start|stop))?")
CONFIRMED_STATES = ("started", "stopped")
VERIFIED_STATUS = "DEV stopped and stock absence re-verified; nothing else touched"
LOGGER = logging.getLogger(__name__)


class LifecycleError(RuntimeError):
    """A sanitized lifecycle failure that must not be treated as success."""


class TransientAvailabilityError(LifecycleError):
    """A documented temporary Supervisor availability failure."""


class Transport(Protocol):
    """Minimal fixed-endpoint Supervisor transport used by the controller."""

    def request(self, method: str, path: str) -> Any:
        """Make one Supervisor API request and return its `data`."""


@dataclass(frozen=True)
class Facts:
    """Validated stock-absent facts, read from the complete installed-app catalog."""

    installed_slugs: tuple[str, ...]
    dev_state: str
    dev_version: str


@dataclass(frozen=True)
class Snapshot:
    """Validated immutable lifecycle state for exactly one run."""

    run_id: str
    nonce: str
    created_at: float
    installed_slugs: tuple[str, ...]
    dev_version: str


class SupervisorTransport:
    """Bounded native-curl transport with no caller-controlled destination."""

    def __init__(self, token: str, deadline: float) -> None:
        """Create a transport with one private token and absolute deadline."""
        if not token or any(char in token for char in "\r\n\x00"):
            raise LifecycleError("SUPERVISOR_TOKEN is required")
        self._token = token
        self._deadline = deadline

    def request(self, method: str, path: str) -> Any:
        """Issue a deadline-bounded fixed Supervisor request and return its `data`."""
        if method not in {"GET", "POST"} or not PATH_PATTERN.fullmatch(path):
            raise LifecycleError("invalid fixed API path")
        raw, status = self._curl(method, f"{SUPERVISOR_BASE_URL}{path}")
        if not 200 <= status < 300:
            raise LifecycleError("Supervisor returned an unsuccessful HTTP status")
        try:
            parsed = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            raise LifecycleError("Supervisor returned malformed JSON") from err
        if not isinstance(parsed, dict) or parsed.get("result") != "ok" or "data" not in parsed:
            raise LifecycleError("Supervisor response is malformed")
        return parsed["data"]

    def _curl(self, method: str, url: str) -> tuple[bytes, int]:
        """Run fixed native curl with credentials only in its stdin configuration."""
        remaining = self._remaining()
        lines = [
            'header = "Accept: application/json"',
            f'url = "{_curl_config_value(url)}"',
            f"max-time = {remaining:.6f}",
            f"max-filesize = {MAX_RESPONSE}",
            'write-out = "\\n%{http_code}"',
            f'header = "Authorization: {_curl_config_value(f"Bearer {self._token}")}"',
            f'request = "{method}"',
        ]
        cfg = ("\n".join(lines) + "\n").encode()
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
        except subprocess.TimeoutExpired as err:
            raise TransientAvailabilityError("HTTP request failed") from err
        except OSError as err:
            raise LifecycleError("HTTP request failed") from err
        if result.returncode == 63:
            raise LifecycleError("HTTP response exceeded limit")
        if result.returncode in {5, 6, 7, 28, 52, 56}:
            raise TransientAvailabilityError("HTTP request failed")
        if result.returncode != 0:
            raise LifecycleError("HTTP request failed")
        raw, separator, status_bytes = result.stdout.rpartition(b"\n")
        if not separator or len(status_bytes) != 3 or not status_bytes.isdigit():
            raise LifecycleError("HTTP response is malformed")
        if len(raw) > MAX_RESPONSE:
            raise LifecycleError("HTTP response exceeded limit")
        return raw, int(status_bytes)

    def _remaining(self) -> float:
        """Return the remaining absolute budget."""
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise LifecycleError("lifecycle deadline expired")
        return remaining


def _curl_config_value(value: str) -> str:
    """Escape one curl config quoted value without permitting control characters."""
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise LifecycleError("HTTP request value is invalid")
    return value.replace("\\", "\\\\").replace('"', '\\"')


def read_facts(transport: Transport) -> Facts:
    """
    Establish stock absence from the complete installed-app catalog, then DEV's own facts.

    Absence is a fact about the whole `GET /addons` list, never an inferred shape of a
    per-app `info` fallback for an uninstalled app.
    """
    catalog = transport.request("GET", "/addons")
    if not isinstance(catalog, dict) or set(catalog) != {"addons"}:
        raise LifecycleError("installed-app catalog is malformed")
    entries = catalog["addons"]
    if not isinstance(entries, list) or not entries:
        raise LifecycleError("installed-app catalog is malformed")
    slugs: list[str] = []
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("slug"), str)
            or not entry["slug"]
            or not isinstance(entry.get("state"), str)
            or not isinstance(entry.get("version"), str)
        ):
            raise LifecycleError("installed-app catalog entry is incomplete")
        slugs.append(entry["slug"])
    if len(set(slugs)) != len(slugs):
        raise LifecycleError("installed-app catalog contains duplicate slugs")
    if STOCK in slugs:
        raise LifecycleError(
            "stock Music Assistant is installed; this lifecycle requires its absence"
        )
    if DEV not in slugs:
        raise LifecycleError("DEV app is not installed")
    if any(slug != DEV and "music_assistant" in slug for slug in slugs):
        raise LifecycleError("an unexpected Music Assistant app is installed")
    info = _dev_info(transport)
    if info["protected"] is not True:
        raise LifecycleError("DEV app protection mode is off")
    if info["boot"] != "manual":
        raise LifecycleError("DEV app boot mode is not manual")
    if info["watchdog"] is not False:
        raise LifecycleError("DEV app Supervisor watchdog is on")
    return Facts(tuple(sorted(slugs)), info["state"], info["version"])


def _dev_info(transport: Transport) -> dict[str, Any]:
    """Read DEV's own facts; the state must be confirmed, never `unknown` or `error`."""
    info = _dev_observation(transport)
    if info["state"] not in CONFIRMED_STATES:
        raise LifecycleError("DEV app state is not confirmed started or stopped")
    return info


def _dev_observation(transport: Transport) -> dict[str, Any]:
    """Read DEV's own info with any string state, for the stop loop that must act on all."""
    info = transport.request("GET", f"/addons/{DEV}/info")
    if not isinstance(info, dict) or info.get("slug") != DEV:
        raise LifecycleError("DEV app response is malformed")
    if not isinstance(info.get("state"), str):
        raise LifecycleError("DEV app state is malformed")
    if not isinstance(info.get("version"), str) or not info["version"]:
        raise LifecycleError("DEV app version is missing")
    return {key: info.get(key) for key in ("state", "version", "protected", "boot", "watchdog")}


def capture(transport: Transport) -> Snapshot:
    """Capture a fresh stock-absent run while DEV is confirmed stopped."""
    facts = read_facts(transport)
    if facts.dev_state != "stopped":
        raise LifecycleError("capture requires DEV stopped")
    return Snapshot(
        run_id=str(uuid.uuid4()),
        nonce=secrets.token_hex(32),
        created_at=time.time(),
        installed_slugs=facts.installed_slugs,
        dev_version=facts.dev_version,
    )


def _require_snapshot_facts(snapshot: Snapshot, facts: Facts) -> None:
    """Refuse to act on a catalog or DEV image other than the captured one."""
    if facts.installed_slugs != snapshot.installed_slugs:
        raise LifecycleError("installed-app catalog changed since capture")
    if facts.dev_version != snapshot.dev_version:
        raise LifecycleError("DEV app version changed since capture")


def start(transport: Transport, snapshot: Snapshot) -> None:
    """Start DEV only inside an armed run whose captured facts still hold."""
    facts = read_facts(transport)
    _require_snapshot_facts(snapshot, facts)
    if facts.dev_state != "stopped":
        raise LifecycleError("start requires DEV stopped")
    transport.request("POST", f"/addons/{DEV}/start")
    if read_facts(transport).dev_state != "started":
        raise LifecycleError("DEV app did not start")


def restore(transport: Transport, snapshot: Snapshot, deadline: float) -> None:
    """
    Stop DEV, confirm by observation, then re-verify stock absence. Nothing else.

    The stop comes first and depends only on DEV's own confirmed state: a catalog that
    has drifted (even stock reappearing) must never leave DEV running.
    """
    _stop_dev(transport, deadline)
    _require_snapshot_facts(snapshot, read_facts(transport))


def _stop_dev(transport: Transport, deadline: float) -> None:
    """
    Drive DEV to an observed exact `stopped`, whatever state it is in, within the budget.

    Any state other than `stopped` (including `startup`, `error`, `unknown` or a transient
    read failure) gets a stop request; transient failures are retried until the deadline.
    A stop response is never evidence; only the observation is.
    """
    _require_deadline(deadline)
    stop_sent_for: str | None = None
    while True:
        if time.monotonic() >= deadline:
            raise LifecycleError("DEV app did not stop")
        try:
            state = _dev_observation(transport)["state"]
        except TransientAvailabilityError:
            state = None
        if state == "stopped":
            return
        if state is not None and state != stop_sent_for:
            # Supervisor may refuse a stop mid-transition or be briefly unavailable;
            # the next observation decides, never the request.
            with suppress(LifecycleError):
                transport.request("POST", f"/addons/{DEV}/stop")
            stop_sent_for = state
            continue
        if time.monotonic() + STOP_POLL_INTERVAL >= deadline:
            raise LifecycleError("DEV app did not stop")
        time.sleep(STOP_POLL_INTERVAL)


def save_snapshot(root: Path, snapshot: Snapshot) -> Path:
    """Persist a new private run with owner-only permissions and no replacements."""
    _assert_safe_root(root)
    run_dir = root / snapshot.run_id
    try:
        run_dir.mkdir(mode=0o700)
    except FileExistsError as err:
        raise LifecycleError("lifecycle run already exists") from err
    _assert_regular_dir(run_dir)
    _write_json_exclusive(
        run_dir / "snapshot.json", {"schema": SCHEMA, "snapshot": _snapshot_dict(snapshot)}
    )
    return run_dir


def load_snapshot(root: Path, run_id: str) -> tuple[Path, Snapshot]:
    """Load only a current-schema, owner-only snapshot below the fixed private root."""
    if not _is_uuid(run_id):
        raise LifecycleError("invalid lifecycle run identity")
    _assert_safe_root(root)
    run_dir = root / run_id
    _assert_regular_dir(run_dir)
    state_path = run_dir / "snapshot.json"
    _assert_regular_file(state_path)
    if state_path.stat().st_size > MAX_RESPONSE:
        raise LifecycleError("snapshot exceeded limit")
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise LifecycleError("snapshot is malformed") from err
    if (
        not isinstance(raw, dict)
        or raw.get("schema") != SCHEMA
        or set(raw) != {"schema", "snapshot"}
    ):
        raise LifecycleError("snapshot schema is unknown")
    return run_dir, _parse_snapshot(raw["snapshot"], expected_run_id=run_id)


def arm_watchdog(root: Path, run_id: str, timeout: float, *, self_test: bool = False) -> int:
    """Detach a one-shot child that invokes this controller's same stop path."""
    run_dir, snapshot = load_snapshot(root, run_id)
    if not 0 <= time.time() - snapshot.created_at <= MAX_RUN_AGE:
        raise LifecycleError("snapshot is too old to arm a new trial watchdog")
    if not math.isfinite(timeout) or not 0 < timeout <= MAX_WATCHDOG_TIMEOUT:
        raise LifecycleError("watchdog timeout is outside the allowed range")
    _reject_completed_run_for_arming(run_dir)
    mode = "self-test-child" if self_test else "watchdog"
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not self_test and not token:
        raise LifecycleError("SUPERVISOR_TOKEN is required")
    child_env = {"SUPERVISOR_TOKEN": token} if not self_test else {}
    lease = _acquire_watchdog_lease(root)
    try:
        # The held lease names its run so `start` can prove a live watchdog for THIS run,
        # not merely a watchdog.json left behind by a failed spawn or a dead child.
        os.ftruncate(lease, 0)
        os.write(lease, json.dumps({"run_id": snapshot.run_id, "nonce": snapshot.nonce}).encode())
        os.fsync(lease)
        _write_json_exclusive(
            run_dir / "watchdog.json",
            {"run_id": snapshot.run_id, "nonce": snapshot.nonce, "deadline": time.time() + timeout},
        )
        if self_test:
            _write_json_exclusive(
                run_dir / "simulation.json", {"run_id": snapshot.run_id, "nonce": snapshot.nonce}
            )
        argv = [
            sys.executable,
            str(Path(__file__).resolve()),
            mode,
            "--run",
            run_id,
            "--root",
            str(root),
        ]
        # Native spawn avoids Python-after-fork deadlocks. DUP2 preserves only the
        # lease across exec without making any parent descriptor inheritable.
        child = os.posix_spawn(
            sys.executable,
            argv,
            child_env,
            setsid=True,
            file_actions=[
                (os.POSIX_SPAWN_OPEN, 0, "/dev/null", os.O_RDONLY, 0o600),
                (os.POSIX_SPAWN_OPEN, 1, "/dev/null", os.O_WRONLY, 0o600),
                (os.POSIX_SPAWN_OPEN, 2, "/dev/null", os.O_WRONLY, 0o600),
                (os.POSIX_SPAWN_DUP2, lease, 198),
            ],
        )
    finally:
        os.close(lease)
    return child


def _reject_completed_run_for_arming(run_dir: Path) -> None:
    """Require a fresh UUID instead of rearming a run with any completion evidence."""
    receipt = run_dir / "receipt.json"
    if receipt.is_symlink() or receipt.exists():
        raise LifecycleError("lifecycle run already has a receipt; capture a fresh run first")


def _require_armed(root: Path, run_dir: Path, snapshot: Snapshot) -> None:
    """Require a live, unexpired watchdog for this exact run before DEV may start."""
    if (run_dir / "simulation.json").exists():
        raise LifecycleError("a self-test run can never start the live DEV app")
    watch = _load_watchdog(run_dir, snapshot)
    if watch["deadline"] <= time.time():
        raise LifecycleError("watchdog deadline already passed; capture and arm a fresh run")
    if (run_dir / "receipt.json").exists():
        raise LifecycleError("lifecycle run already has a receipt")
    _require_live_lease(root, snapshot)


def _require_live_lease(root: Path, snapshot: Snapshot) -> None:
    """Prove a detached child still holds the lease and that it names this run."""
    descriptor = os.open(
        root / "watchdog.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
    )
    try:
        _assert_private_lease(descriptor)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            raise LifecycleError("no live watchdog holds the lease; arm a fresh run first")
        if os.fstat(descriptor).st_size > MAX_RESPONSE:
            raise LifecycleError("watchdog lease is malformed")
        try:
            owner = json.loads(os.pread(descriptor, MAX_RESPONSE, 0))
        except (OSError, ValueError) as err:
            raise LifecycleError("watchdog lease is malformed") from err
        if owner != {"run_id": snapshot.run_id, "nonce": snapshot.nonce}:
            raise LifecycleError("the live watchdog belongs to another run")
    finally:
        os.close(descriptor)


def watchdog(root: Path, run_id: str, transport: Transport | None = None) -> None:
    """Validate watchdog ownership, wait until its deadline, then stop DEV and receipt."""
    run_dir, snapshot = load_snapshot(root, run_id)
    watch = _load_watchdog(run_dir, snapshot)
    delay = watch["deadline"] - time.time()
    if delay > 0:
        time.sleep(delay)
    _settle(root, run_dir, snapshot, transport)


def _settle(root: Path, run_dir: Path, snapshot: Snapshot, transport: Transport | None) -> None:
    """
    Run the single stop path under the mutation lock and record its receipt.

    Waiting for the lock has its own budget; the stop gets a full restore budget only
    once the lock is held, so a slow `start` holding the lock cannot starve the stop.
    A lock that never frees is itself recorded as a failed receipt.
    """
    try:
        lock = _mutation_lock(root, time.monotonic() + LOCK_TIMEOUT)
        lock.__enter__()
    except LifecycleError as err:
        _write_receipt(run_dir, snapshot, False, f"mutation lock unavailable: {err}")
        raise
    try:
        if _has_verified_receipt(run_dir, snapshot):
            return
        deadline = time.monotonic() + RESTORE_TIMEOUT
        active_transport = transport or SupervisorTransport(
            os.environ.get("SUPERVISOR_TOKEN", ""), deadline
        )
        try:
            restore(active_transport, snapshot, deadline)
        except LifecycleError as err:
            _write_receipt(run_dir, snapshot, False, str(err))
            raise
        _write_receipt(run_dir, snapshot, True, VERIFIED_STATUS)
    finally:
        lock.__exit__(None, None, None)


def self_test(root: Path) -> None:
    """Run a nondestructive detached-child and receipt check using only fake transport."""
    snapshot = Snapshot(
        str(uuid.uuid4()), secrets.token_hex(32), time.time(), ("core_ssh", DEV), "2.11.0.dev"
    )
    run_dir = save_snapshot(root, snapshot)
    arm_watchdog(root, snapshot.run_id, 0.05, self_test=True)
    receipt = run_dir / "receipt.json"
    until = time.monotonic() + 5.0
    while time.monotonic() < until:
        if receipt.exists():
            _assert_regular_file(receipt)
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            if payload.get("run_id") == snapshot.run_id and payload.get("verified") is True:
                return
            raise LifecycleError("self-test child returned an invalid receipt")
        time.sleep(0.02)
    raise LifecycleError("self-test watchdog receipt timed out")


def _snapshot_dict(snapshot: Snapshot) -> dict[str, Any]:
    """Serialize a snapshot with list fields for JSON."""
    value = asdict(snapshot)
    value["installed_slugs"] = list(snapshot.installed_slugs)
    return value


def _parse_snapshot(value: Any, expected_run_id: str) -> Snapshot:
    """Validate an untrusted snapshot structure exactly."""
    if not isinstance(value, dict) or set(value) != {
        "run_id",
        "nonce",
        "created_at",
        "installed_slugs",
        "dev_version",
    }:
        raise LifecycleError("snapshot is malformed")
    slugs = value["installed_slugs"]
    if (
        value["run_id"] != expected_run_id
        or not isinstance(value["nonce"], str)
        or not _is_nonce(value["nonce"])
        or type(value["created_at"]) not in (int, float)
        or not math.isfinite(value["created_at"])
        or not isinstance(slugs, list)
        or not slugs
        or not all(isinstance(slug, str) and slug for slug in slugs)
        or slugs != sorted(set(slugs))
        or STOCK in slugs
        or DEV not in slugs
        or not isinstance(value["dev_version"], str)
        or not value["dev_version"]
    ):
        raise LifecycleError("snapshot is malformed")
    return Snapshot(
        value["run_id"],
        value["nonce"],
        float(value["created_at"]),
        tuple(slugs),
        value["dev_version"],
    )


def _assert_safe_root(root: Path) -> None:
    """Create or validate the private, owner-only lifecycle directory."""
    parent = root.parent
    if parent.is_symlink() or not parent.is_dir():
        raise LifecycleError("lifecycle root parent is unsafe")
    if root.is_symlink():
        raise LifecycleError("lifecycle root must not be a symlink")
    root.mkdir(mode=0o700, exist_ok=True)
    _assert_regular_dir(root)


def _assert_regular_dir(path: Path) -> None:
    """Reject redirected directories and directories accessible by another user."""
    if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.getuid():
        raise LifecycleError("lifecycle directory is unsafe")
    if path.stat().st_mode & 0o077:
        raise LifecycleError("lifecycle directory permissions are unsafe")


def _assert_regular_file(path: Path) -> None:
    """Require a private regular file owned by this process's user."""
    if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid():
        raise LifecycleError("lifecycle file is unsafe")
    if path.stat().st_mode & 0o077:
        raise LifecycleError("lifecycle file permissions are unsafe")


def _write_json_exclusive(path: Path, value: dict[str, Any]) -> None:
    """Write new private state without replacing an existing run artifact."""
    payload = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as err:
        raise LifecycleError("could not create private lifecycle state") from err
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _load_watchdog(run_dir: Path, snapshot: Snapshot) -> dict[str, Any]:
    """Validate the deadline and nonce belonging to this exact armed run."""
    path = run_dir / "watchdog.json"
    _assert_regular_file(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise LifecycleError("watchdog state is malformed") from err
    if (
        not isinstance(value, dict)
        or set(value) != {"run_id", "nonce", "deadline"}
        or value["run_id"] != snapshot.run_id
        or value["nonce"] != snapshot.nonce
        or type(value["deadline"]) not in (int, float)
        or not math.isfinite(value["deadline"])
        or value["deadline"] > time.time() + MAX_WATCHDOG_TIMEOUT
    ):
        raise LifecycleError("watchdog does not own this lifecycle run")
    return value


@contextmanager
def _mutation_lock(root: Path, deadline: float) -> Generator[None]:
    """Serialize mutations while consuming only the caller's absolute budget."""
    descriptor = os.open(
        root / "mutation.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
    )
    try:
        _assert_private_lease(descriptor)
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                _require_deadline(deadline)
                time.sleep(0.1)
        yield
    finally:
        os.close(descriptor)


def _acquire_watchdog_lease(root: Path) -> int:
    """Keep one application-wide lease inherited by the detached child."""
    descriptor = os.open(
        root / "watchdog.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
    )
    try:
        _assert_private_lease(descriptor)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as err:
            raise LifecycleError("a lifecycle watchdog is already armed") from err
    except (OSError, LifecycleError):
        os.close(descriptor)
        raise
    return descriptor


def _assert_private_lease(descriptor: int) -> None:
    """Validate the opened lease itself rather than a racy pathname lookup."""
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise LifecycleError("watchdog lease is unsafe")


def _has_verified_receipt(run_dir: Path, snapshot: Snapshot) -> bool:
    """Skip a settled run without letting an invalid receipt suppress the stop."""
    path = run_dir / "receipt.json"
    if not path.exists():
        return False
    try:
        _assert_regular_file(path)
        if path.stat().st_size > MAX_RESPONSE:
            return False
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, LifecycleError):
        return False
    return bool(
        receipt
        == {
            "run_id": snapshot.run_id,
            "nonce": snapshot.nonce,
            "verified": True,
            "status": VERIFIED_STATUS,
        }
    )


def _write_receipt(run_dir: Path, snapshot: Snapshot, verified: bool, status: str) -> None:
    """Record the stop outcome, preserving failed evidence before promotion."""
    path = run_dir / "receipt.json"
    receipt = {
        "run_id": snapshot.run_id,
        "nonce": snapshot.nonce,
        "verified": verified,
        "status": status,
    }
    if path.exists():
        _assert_regular_file(path)
        if path.stat().st_size > MAX_RESPONSE:
            raise LifecycleError("lifecycle receipt exceeded limit")
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as err:
            raise LifecycleError("lifecycle receipt is malformed") from err
        if existing == receipt:
            return
        if (
            not verified
            and isinstance(existing, dict)
            and existing.get("run_id") == snapshot.run_id
            and existing.get("nonce") == snapshot.nonce
            and existing.get("verified") is False
        ):
            # The first failure stays the evidence of record; a repeat failure must not
            # mask its own cause behind a receipt conflict.
            return
        if not (
            verified
            and isinstance(existing, dict)
            and set(existing) == {"run_id", "nonce", "verified", "status"}
            and existing.get("run_id") == snapshot.run_id
            and existing.get("nonce") == snapshot.nonce
            and existing.get("verified") is False
            and isinstance(existing.get("status"), str)
        ):
            raise LifecycleError("lifecycle receipt conflicts with this stop")
        _archive_failed_receipt(run_dir / "failed_receipt.json", existing)
        _replace_json(path, receipt)
        return
    _write_json_exclusive(path, receipt)


def _archive_failed_receipt(path: Path, expected: dict[str, Any]) -> None:
    """Preserve failed evidence idempotently across an interrupted promotion."""
    if not path.exists():
        _write_json_exclusive(path, expected)
        return
    _assert_regular_file(path)
    if path.stat().st_size > MAX_RESPONSE:
        raise LifecycleError("failed lifecycle evidence exceeded limit")
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise LifecycleError("failed lifecycle evidence is malformed") from err
    if existing != expected:
        raise LifecycleError("failed lifecycle evidence conflicts with this stop")


def _replace_json(path: Path, value: dict[str, Any]) -> None:
    """Atomically replace a validated receipt only inside its private run directory."""
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    _write_json_exclusive(temporary, value)
    try:
        temporary.replace(path)
    except OSError as err:
        temporary.unlink(missing_ok=True)
        raise LifecycleError("could not replace private lifecycle receipt") from err


def _require_deadline(deadline: float) -> None:
    """Refuse further work once the absolute budget expires."""
    if time.monotonic() >= deadline:
        raise LifecycleError("lifecycle deadline expired")


def _is_uuid(value: object) -> bool:
    """Return whether an untrusted value is a canonical UUID string."""
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _is_nonce(value: str) -> bool:
    """Return whether the stored nonce is a full lowercase hexadecimal secret."""
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _require_run_id(run_id: str | None) -> str:
    """Return a required run identity for commands that operate on saved state."""
    if not run_id:
        raise LifecycleError("--run is required")
    return run_id


def _simulation_transport(root: Path, run_id: str) -> FakeTransport:
    """Create fake child transport only for the exact armed self-test run."""
    run_dir, snapshot = load_snapshot(root, run_id)
    path = run_dir / "simulation.json"
    _assert_regular_file(path)
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise LifecycleError("simulation marker is malformed") from err
    if marker != {"run_id": snapshot.run_id, "nonce": snapshot.nonce}:
        raise LifecycleError("simulation marker does not own this lifecycle run")
    return FakeTransport(dev_started=True)


class FakeTransport:
    """In-memory transport only for tests and the CLI's isolated self-test child."""

    def __init__(
        self, *, dev_started: bool = False, slugs: tuple[str, ...] = (DEV, "core_ssh")
    ) -> None:
        """Initialize a stock-absent catalog with DEV and one unrelated app."""
        self.dev_started = dev_started
        self.slugs = list(slugs)
        self.dev_version = "2.11.0.dev"
        self.protected = True
        self.boot = "manual"
        self.watchdog = False
        self.calls: list[str] = []

    def request(self, method: str, path: str) -> Any:
        """Model the fixed calls made by the lifecycle, without contacting HAOS."""
        self.calls.append(f"{method} {path}")
        if path == "/addons":
            return {
                "addons": [
                    {
                        "slug": slug,
                        "state": self._dev_state() if slug == DEV else "started",
                        "version": self.dev_version if slug == DEV else "1.0.0",
                    }
                    for slug in self.slugs
                ]
            }
        if path == f"/addons/{DEV}/info":
            return {
                "slug": DEV,
                "state": self._dev_state(),
                "version": self.dev_version,
                "protected": self.protected,
                "boot": self.boot,
                "watchdog": self.watchdog,
            }
        if path == f"/addons/{DEV}/stop":
            self.dev_started = False
            return None
        if path == f"/addons/{DEV}/start":
            self.dev_started = True
            return None
        raise LifecycleError("self-test fake transport rejected request")

    def _dev_state(self) -> str:
        return "started" if self.dev_started else "stopped"


def main(argv: list[str] | None = None) -> int:
    """Run one explicit private lifecycle operation."""
    parser = argparse.ArgumentParser(description="Private stock-absent DEV lifecycle controller")
    parser.add_argument(
        "command",
        choices=(
            "preflight",
            "capture",
            "arm",
            "start",
            "stop",
            "watchdog",
            "self-test",
            "self-test-child",
        ),
    )
    parser.add_argument("--run")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)

    def live_transport() -> SupervisorTransport:
        return SupervisorTransport(
            os.environ.get("SUPERVISOR_TOKEN", ""), time.monotonic() + RESTORE_TIMEOUT
        )

    try:
        if args.command == "self-test":
            self_test(args.root)
        elif args.command == "preflight":
            facts = read_facts(live_transport())
            sys.stdout.write(json.dumps(asdict(facts), sort_keys=True) + "\n")
        elif args.command == "capture":
            sys.stdout.write(f"{save_snapshot(args.root, capture(live_transport()))}\n")
        else:
            run_id = _require_run_id(args.run)
            if args.command == "arm":
                sys.stdout.write(f"{arm_watchdog(args.root, run_id, args.timeout)}\n")
            elif args.command == "start":
                run_dir, snapshot = load_snapshot(args.root, run_id)
                deadline = time.monotonic() + RESTORE_TIMEOUT
                with _mutation_lock(args.root, deadline):
                    _require_armed(args.root, run_dir, snapshot)
                    start(live_transport(), snapshot)
            elif args.command == "stop":
                run_dir, snapshot = load_snapshot(args.root, run_id)
                _settle(args.root, run_dir, snapshot, None)
            elif args.command == "self-test-child":
                watchdog(args.root, run_id, _simulation_transport(args.root, run_id))
            else:
                watchdog(args.root, run_id)
    except LifecycleError as err:
        LOGGER.error(f"Lifecycle failed: {err}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
