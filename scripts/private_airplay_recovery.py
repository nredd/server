"""
Private, fail-closed recovery controller for the native AirPlay trial.

References:
- https://developers.home-assistant.io/docs/api/supervisor/endpoints/
- https://developers.home-assistant.io/docs/api/rest/

This file is deliberately private trial tooling. It runs on the SSH app's Python 3.13,
independently of MA's Python 3.14 environment. It neither installs nor starts the DEV
add-on; callers arm the independent watchdog before doing any disruptive work.
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
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

ROOT = Path("/data/airplay-native-probe/recovery")
OFFICIAL = "d5369777_music_assistant"
OFFICIAL_VERSION = "2.10.7"
DEV = "d5369777_music_assistant_dev"
TARGETS = ("media_player.mainpod", "media_player.officepod")
CAPTURE_TARGETS = {"main": (TARGETS[0],), "office": (TARGETS[1],), "both": TARGETS}
MAX_RESPONSE = 64 * 1024
ACTION_TIMEOUT = 30.0
RESTORE_TIMEOUT = 90.0
GROUP_RECONCILE_INTERVAL = 5.0
VOLUME_TOLERANCE = 0.015
SCHEMA = 1
MAX_RUN_AGE = 600.0
SUPERVISOR_BASE_URL = "http://supervisor"
PUBLIC_INFO_URL = "http://localhost:8095/info"
PATH_PATTERN = re.compile(
    rf"/(?:addons/(?:{OFFICIAL}|{DEV})/(?:info|start|stop)|core/api/(?:template|states/media_player\.[a-z0-9_]+|services/(?:homeassistant/reload_config_entry|media_player/(?:unjoin|join|volume_set|media_stop|media_pause|turn_off|media_play))))"
)
LOGGER = logging.getLogger(__name__)


class RecoveryError(RuntimeError):
    """A sanitized recovery failure that must not be treated as success."""


class TransientAvailabilityError(RecoveryError):
    """A documented temporary Core or public API availability failure."""


class Transport(Protocol):
    """Minimal fixed-endpoint transport used by the controller."""

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        """Make one Supervisor or Core API request."""

    def public_info(self) -> None:
        """Read the fixed local MA public readiness endpoint."""


@dataclass(frozen=True)
class PlayerState:
    """Minimal state necessary to restore one explicitly approved target."""

    entity_id: str
    state: str
    volume: float | None
    group_members: tuple[str, ...]


@dataclass(frozen=True)
class Snapshot:
    """Validated immutable restoration state for exactly one run."""

    run_id: str
    nonce: str
    created_at: float
    official_started: bool
    official_protected: bool
    official_version: str
    dev_started: bool
    players: tuple[PlayerState, ...]


class SupervisorTransport:
    """Bounded native-curl transport with no caller-controlled destination."""

    def __init__(self, token: str, deadline: float) -> None:
        """Create a transport with one private token and absolute deadline."""
        if not token or any(char in token for char in "\r\n\x00"):
            raise RecoveryError("SUPERVISOR_TOKEN is required")
        self._token = token
        self._deadline = deadline

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        """Issue a deadline-bounded fixed Supervisor request and parse its JSON response."""
        if method not in {"GET", "POST"} or not PATH_PATTERN.fullmatch(path):
            raise RecoveryError("invalid fixed API path")
        raw, status = self._curl(
            method,
            f"{SUPERVISOR_BASE_URL}{path}",
            body,
            authorization=f"Bearer {self._token}",
        )
        if not 200 <= status < 300:
            if path.startswith("/core/api/states/") and status in {404, 502, 503}:
                raise TransientAvailabilityError("Core target state is temporarily unavailable")
            raise RecoveryError("Supervisor returned an unsuccessful HTTP status")
        parsed = _parse_json_response(raw, "Supervisor")
        if path.startswith("/addons/"):
            if not isinstance(parsed, dict) or parsed.get("result") != "ok" or "data" not in parsed:
                raise RecoveryError("Supervisor add-on response is malformed")
            return parsed["data"]
        return parsed

    def public_info(self) -> None:
        """Read the fixed local MA `/info` readiness endpoint without credentials."""
        raw, status = self._curl("GET", PUBLIC_INFO_URL, None)
        if status in {404, 502, 503}:
            raise TransientAvailabilityError("MA public API is temporarily unavailable")
        if not 200 <= status < 300:
            raise RecoveryError("MA public API returned an unsuccessful HTTP status")
        payload = _parse_json_response(raw, "MA public API")
        if not isinstance(payload, dict) or payload.get("server_version") != OFFICIAL_VERSION:
            raise RecoveryError("MA public API identity is not the approved version")

    def _curl(
        self,
        method: str,
        url: str,
        body: dict[str, Any] | None,
        *,
        authorization: str | None = None,
    ) -> tuple[bytes, int]:
        """Run fixed native curl with credentials only in its stdin configuration."""
        remaining = self._remaining()
        lines = [
            'header = "Accept: application/json"',
            f'url = "{_curl_config_value(url)}"',
            f"max-time = {remaining:.6f}",
            f"max-filesize = {MAX_RESPONSE}",
            'write-out = "\\n%{http_code}"',
        ]
        if authorization is not None:
            lines.append(f'header = "Authorization: {_curl_config_value(authorization)}"')
        if body is not None:
            payload = json.dumps(body, separators=(",", ":"))
            lines.extend(
                (
                    'header = "Content-Type: application/json"',
                    f'data = "{_curl_config_value(payload)}"',
                )
            )
        lines.append(f'request = "{method}"')
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
            raise RecoveryError("HTTP request failed") from err
        if result.returncode == 63:
            raise RecoveryError("HTTP response exceeded limit")
        if result.returncode in {5, 6, 7, 28, 52, 56}:
            raise TransientAvailabilityError("HTTP request failed")
        if result.returncode != 0:
            raise RecoveryError("HTTP request failed")
        raw, separator, status_bytes = result.stdout.rpartition(b"\n")
        if not separator or len(status_bytes) != 3 or not status_bytes.isdigit():
            raise RecoveryError("HTTP response is malformed")
        if len(raw) > MAX_RESPONSE:
            raise RecoveryError("HTTP response exceeded limit")
        return raw, int(status_bytes)

    def _remaining(self) -> float:
        """Return remaining absolute recovery budget."""
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise RecoveryError("recovery deadline expired")
        return remaining


def _curl_config_value(value: str) -> str:
    """Escape one curl config quoted value without permitting control characters."""
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise RecoveryError("HTTP request value is invalid")
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _parse_json_response(raw: bytes, service: str) -> Any:
    """Parse a bounded curl response without retaining untrusted response text in errors."""
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise RecoveryError(f"{service} returned malformed JSON") from err


def capture(transport: Transport, targets: tuple[str, ...] = TARGETS) -> Snapshot:
    """Capture a minimal, fresh restoration snapshot after safe preflight."""
    _assert_targets(targets)
    official = _addon_info(transport, OFFICIAL)
    dev = _addon_info(transport, DEV)
    _assert_official(official)
    if not _addon_started(official) or _addon_started(dev):
        raise RecoveryError("capture requires official MA started and DEV stopped")
    _assert_no_other_streams(transport, targets)
    players = tuple(_player_state(transport, entity_id) for entity_id in targets)
    if any(player.state not in {"idle", "off", "paused", "playing"} for player in players):
        raise RecoveryError("target play state is unsupported")
    _assert_target_groups(players, targets)
    return Snapshot(
        run_id=str(uuid.uuid4()),
        nonce=secrets.token_hex(32),
        created_at=time.time(),
        official_started=_addon_started(official),
        official_protected=official["protected"],
        official_version=official["version"],
        dev_started=_addon_started(dev),
        players=players,
    )


def restore(transport: Transport, snapshot: Snapshot, deadline: float) -> None:
    """Fail closed while restoring the DEV/official application and target state."""
    _require_deadline(deadline)
    _stop_dev(transport)
    if _start_official(transport):
        _wait_for_public_api(transport, deadline)
        # Reload the EXISTING entry, preserving its configuration and credentials.
        # Otherwise HA's reconnect backoff can outlive this recovery deadline.
        transport.request(
            "POST",
            "/core/api/services/homeassistant/reload_config_entry",
            {"entity_id": snapshot.players[0].entity_id},
        )
    _wait_for_targets(transport, snapshot.players, deadline)
    _restore_groups(transport, snapshot.players)
    _restore_volumes(transport, snapshot.players)
    _restore_playstate(transport, snapshot.players)
    _verify(transport, snapshot.players, deadline)


def save_snapshot(root: Path, snapshot: Snapshot) -> Path:
    """Persist a new private run with owner-only permissions and no replacements."""
    _assert_safe_root(root)
    run_dir = root / snapshot.run_id
    try:
        run_dir.mkdir(mode=0o700)
    except FileExistsError as err:
        raise RecoveryError("recovery run already exists") from err
    _assert_regular_dir(run_dir)
    state_path = run_dir / "snapshot.json"
    _write_json_exclusive(state_path, {"schema": SCHEMA, "snapshot": _snapshot_dict(snapshot)})
    return run_dir


def load_snapshot(root: Path, run_id: str) -> tuple[Path, Snapshot]:
    """Load only a current-schema, owner-only snapshot below the fixed private root."""
    if not _is_uuid(run_id):
        raise RecoveryError("invalid recovery run identity")
    _assert_safe_root(root)
    run_dir = root / run_id
    _assert_regular_dir(run_dir)
    state_path = run_dir / "snapshot.json"
    _assert_regular_file(state_path)
    if state_path.stat().st_size > MAX_RESPONSE:
        raise RecoveryError("snapshot exceeded limit")
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise RecoveryError("snapshot is malformed") from err
    if (
        not isinstance(raw, dict)
        or raw.get("schema") != SCHEMA
        or set(raw) != {"schema", "snapshot"}
    ):
        raise RecoveryError("snapshot schema is unknown")
    return run_dir, _parse_snapshot(raw["snapshot"], expected_run_id=run_id)


def arm_watchdog(root: Path, run_id: str, timeout: float, *, self_test: bool = False) -> int:
    """Detach a one-shot child that invokes this controller's same restore path."""
    run_dir, snapshot = load_snapshot(root, run_id)
    if not 0 <= time.time() - snapshot.created_at <= MAX_RUN_AGE:
        raise RecoveryError("snapshot is too old to arm a new trial watchdog")
    if not math.isfinite(timeout) or not 0 < timeout <= 600:
        raise RecoveryError("watchdog timeout is outside the allowed range")
    _reject_completed_run_for_arming(run_dir)
    mode = "self-test-child" if self_test else "watchdog"
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not self_test and not token:
        raise RecoveryError("SUPERVISOR_TOKEN is required")
    child_env = {"SUPERVISOR_TOKEN": token} if not self_test else {}
    lease = _acquire_watchdog_lease(root)
    try:
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
        raise RecoveryError("recovery run already has a receipt; capture a fresh run before arming")


def watchdog(root: Path, run_id: str, transport: Transport | None = None) -> None:
    """Validate watchdog ownership, wait until its deadline, then restore and receipt."""
    run_dir, snapshot = load_snapshot(root, run_id)
    watch = _load_watchdog(run_dir, snapshot)
    delay = watch["deadline"] - time.time()
    if delay > 0:
        time.sleep(delay)
    deadline = time.monotonic() + RESTORE_TIMEOUT
    with _mutation_lock(root, deadline):
        if _has_verified_receipt(run_dir, snapshot):
            return
        active_transport = transport or SupervisorTransport(
            os.environ.get("SUPERVISOR_TOKEN", ""), deadline
        )
        try:
            restore(active_transport, snapshot, deadline)
        except RecoveryError as err:
            _write_receipt(run_dir, snapshot, False, str(err))
            raise
        _write_receipt(run_dir, snapshot, True, "restoration verified")


def self_test(root: Path) -> None:
    """Run a nondestructive detached-child and receipt check using only fake transport."""
    snapshot = _self_test_snapshot()
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
            raise RecoveryError("self-test child returned an invalid receipt")
        time.sleep(0.02)
    raise RecoveryError("self-test watchdog receipt timed out")


def _addon_info(transport: Transport, slug: str) -> dict[str, Any]:
    """Normalize the installed-app and uninstalled-catalog response shapes."""
    data = transport.request("GET", f"/addons/{slug}/info")
    if not isinstance(data, dict):
        raise RecoveryError("add-on response is malformed")
    installed = data.get("installed", True)
    state = data.get("state")
    protected = data.get("protected")
    version = data.get("version")
    if type(installed) is not bool or not isinstance(state, str):
        raise RecoveryError("add-on response is incomplete")
    if installed:
        if type(protected) is not bool or not isinstance(version, str) or not version:
            raise RecoveryError("installed add-on response is incomplete")
    elif state != "unknown" or protected is not None or version is not None:
        raise RecoveryError("uninstalled add-on response is incomplete")
    return {"installed": installed, "state": state, "protected": protected, "version": version}


def _assert_official(info: dict[str, Any]) -> None:
    """Require the exact protected production app before using its recovery route."""
    if (
        info["installed"] is not True
        or info["protected"] is not True
        or info["version"] != OFFICIAL_VERSION
    ):
        raise RecoveryError("official add-on identity is not the approved protected version")


def _addon_started(info: dict[str, Any]) -> bool:
    """Require confirmed installed state; false means exactly stopped, never unknown."""
    if info.get("installed") is not True or info.get("state") not in ("started", "stopped"):
        raise RecoveryError("add-on state is not confirmed started or stopped")
    return bool(info["state"] == "started")


def _assert_no_other_streams(transport: Transport, targets: tuple[str, ...]) -> None:
    """Refuse a window that would interrupt a non-target MA stream."""
    data = transport.request(
        "POST",
        "/core/api/template",
        {"template": "{{ integration_entities('music_assistant') | tojson }}"},
    )
    if isinstance(data, str):
        if len(data.encode()) > MAX_RESPONSE:
            raise RecoveryError("music-assistant entity inventory is malformed")
        try:
            entities = json.loads(data)
        except json.JSONDecodeError as err:
            raise RecoveryError("music-assistant entity inventory is malformed") from err
    else:
        entities = data
    if not isinstance(entities, list) or not all(isinstance(entity, str) for entity in entities):
        raise RecoveryError("music-assistant entity inventory is malformed")
    for entity in entities:
        if entity in targets or not entity.startswith("media_player."):
            continue
        state = _player_state(transport, entity).state
        if state in {"playing", "paused", "buffering"}:
            raise RecoveryError("another Music Assistant stream is active")


def _player_state(transport: Transport, entity_id: str) -> PlayerState:
    """Parse only the HA fields used for recovery, including transient availability."""
    data = transport.request("GET", f"/core/api/states/{entity_id}")
    if not isinstance(data, dict) or data.get("entity_id") != entity_id:
        raise RecoveryError("target state is malformed")
    state = data.get("state")
    attrs = data.get("attributes")
    if not isinstance(state, str) or not isinstance(attrs, dict):
        raise RecoveryError("target state is malformed")
    volume = attrs.get("volume_level")
    if volume is not None and (type(volume) not in (int, float) or not 0 <= volume <= 1):
        raise RecoveryError("target volume is malformed")
    members = attrs.get("group_members", [])
    if not isinstance(members, list) or not all(isinstance(item, str) for item in members):
        raise RecoveryError("target group is malformed")
    if len(members) != len(set(members)):
        raise RecoveryError("target group contains duplicate members")
    return PlayerState(
        entity_id, state, float(volume) if volume is not None else None, tuple(members)
    )


def _assert_target_groups(players: tuple[PlayerState, ...], targets: tuple[str, ...]) -> None:
    """Reject groups whose restoration would mutate an uncaptured player."""
    for player in players:
        if player.group_members and (
            player.entity_id not in player.group_members or set(player.group_members) - set(targets)
        ):
            raise RecoveryError("target group is not exclusively recoverable")


def _stop_dev(transport: Transport) -> None:
    """Confirm DEV is stopped before the original app can reclaim its ports."""
    info = _addon_info(transport, DEV)
    if _addon_started(info):
        transport.request("POST", f"/addons/{DEV}/stop")
    if _addon_started(_addon_info(transport, DEV)):
        raise RecoveryError("DEV add-on did not stop")


def _start_official(transport: Transport) -> bool:
    """Start the approved app if needed and report whether HA must reconnect."""
    info = _addon_info(transport, OFFICIAL)
    _assert_official(info)
    started = not _addon_started(info)
    if started:
        transport.request("POST", f"/addons/{OFFICIAL}/start")
    if not _addon_started(_addon_info(transport, OFFICIAL)):
        raise RecoveryError("official add-on did not start")
    return started


def _wait_for_public_api(transport: Transport, deadline: float) -> None:
    """Wait only for documented transient fixed-endpoint MA public API readiness."""
    while True:
        try:
            transport.public_info()
            return
        except TransientAvailabilityError:
            _require_deadline(deadline)
            time.sleep(0.5)


def _wait_for_targets(
    transport: Transport, expected: tuple[PlayerState, ...], deadline: float
) -> None:
    """Wait only for documented transient Core target state availability."""
    while True:
        try:
            states = tuple(_player_state(transport, player.entity_id).state for player in expected)
        except TransientAvailabilityError:
            _require_deadline(deadline)
            time.sleep(0.5)
            continue
        if all(state not in {"unavailable", "unknown"} for state in states):
            return
        _require_deadline(deadline)
        time.sleep(0.5)


def _restore_groups(transport: Transport, players: tuple[PlayerState, ...]) -> None:
    """Restore only changed HA group topology, preserving captured member order."""
    actual = tuple(_player_state(transport, player.entity_id) for player in players)
    _assert_target_groups(actual, tuple(player.entity_id for player in players))
    changed = tuple(
        (expected, observed)
        for expected, observed in zip(players, actual, strict=True)
        if expected.group_members != observed.group_members
    )
    if not changed:
        return
    for _, observed in changed:
        if observed.group_members:
            transport.request(
                "POST", "/core/api/services/media_player/unjoin", {"entity_id": observed.entity_id}
            )
    for members in {
        expected.group_members for expected, _ in changed if len(expected.group_members) > 1
    }:
        transport.request(
            "POST",
            "/core/api/services/media_player/join",
            {"entity_id": members[0], "group_members": list(members[1:])},
        )


def _restore_volumes(transport: Transport, players: tuple[PlayerState, ...]) -> None:
    """Refuse to overwrite an original HA level with a trial-derived target."""
    for player in players:
        actual = _player_state(transport, player.entity_id)
        if player.volume is not None and (
            actual.volume is None or abs(player.volume - actual.volume) > VOLUME_TOLERANCE
        ):
            raise RecoveryError("stored HA volume differs from the private snapshot")


def _restore_playstate(transport: Transport, players: tuple[PlayerState, ...]) -> None:
    """Restore only play states that no longer match the captured HA state."""
    for player in players:
        if _player_state(transport, player.entity_id).state == player.state:
            continue
        if player.state == "playing":
            transport.request(
                "POST",
                "/core/api/services/media_player/media_play",
                {"entity_id": player.entity_id},
            )
        elif player.state == "paused":
            transport.request(
                "POST",
                "/core/api/services/media_player/media_pause",
                {"entity_id": player.entity_id},
            )
        elif player.state == "idle":
            transport.request(
                "POST",
                "/core/api/services/media_player/media_stop",
                {"entity_id": player.entity_id},
            )
        elif player.state == "off":
            transport.request(
                "POST", "/core/api/services/media_player/turn_off", {"entity_id": player.entity_id}
            )
        else:
            raise RecoveryError("snapshot contains unsupported play state")


def _verify(transport: Transport, expected: tuple[PlayerState, ...], deadline: float) -> None:
    """Reconcile a delayed captured HA group without extending the absolute budget."""
    next_group_reconcile = time.monotonic() + GROUP_RECONCILE_INTERVAL
    targets = tuple(player.entity_id for player in expected)
    while True:
        _require_deadline(deadline)
        actual = tuple(_player_state(transport, player.entity_id) for player in expected)
        _require_deadline(deadline)
        _assert_target_groups(actual, targets)
        if all(_matches(want, got) for want, got in zip(expected, actual, strict=True)):
            return
        if time.monotonic() >= next_group_reconcile and any(
            want.group_members != got.group_members
            for want, got in zip(expected, actual, strict=True)
        ):
            _restore_groups(transport, expected)
            next_group_reconcile = time.monotonic() + GROUP_RECONCILE_INTERVAL
        time.sleep(0.5)


def _matches(expected: PlayerState, actual: PlayerState) -> bool:
    """Compare HA state without treating it as native receiver telemetry."""
    if expected.state != actual.state or expected.group_members != actual.group_members:
        return False
    return expected.volume is None or (
        actual.volume is not None and abs(expected.volume - actual.volume) <= VOLUME_TOLERANCE
    )


def _snapshot_dict(snapshot: Snapshot) -> dict[str, Any]:
    """Convert the validated snapshot into its private JSON representation."""
    return asdict(snapshot)


def _parse_snapshot(value: Any, expected_run_id: str) -> Snapshot:
    """Validate the stored schema and its fixed target ownership."""
    if not isinstance(value, dict) or set(value) != {
        "run_id",
        "nonce",
        "created_at",
        "official_started",
        "official_protected",
        "official_version",
        "dev_started",
        "players",
    }:
        raise RecoveryError("snapshot schema is unknown")
    players = value["players"]
    if not isinstance(players, list) or len(players) not in {1, len(TARGETS)}:
        raise RecoveryError("snapshot players are malformed")
    if not all(
        isinstance(item, dict)
        and set(item) == {"entity_id", "state", "volume", "group_members"}
        and isinstance(item["entity_id"], str)
        and isinstance(item["state"], str)
        and item["state"] in {"idle", "off", "paused", "playing"}
        and (item["volume"] is None or type(item["volume"]) in (int, float))
        and (item["volume"] is None or 0 <= item["volume"] <= 1)
        and isinstance(item["group_members"], list)
        and all(isinstance(member, str) for member in item["group_members"])
        and len(item["group_members"]) == len(set(item["group_members"]))
        for item in players
    ):
        raise RecoveryError("snapshot players are malformed")
    if (
        not isinstance(value["run_id"], str)
        or not isinstance(value["nonce"], str)
        or type(value["created_at"]) not in (int, float)
        or not math.isfinite(value["created_at"])
        or type(value["official_started"]) is not bool
        or type(value["official_protected"]) is not bool
        or not isinstance(value["official_version"], str)
        or type(value["dev_started"]) is not bool
    ):
        raise RecoveryError("snapshot schema is unknown")
    snapshot = Snapshot(
        value["run_id"],
        value["nonce"],
        float(value["created_at"]),
        value["official_started"],
        value["official_protected"],
        value["official_version"],
        value["dev_started"],
        tuple(
            PlayerState(
                item["entity_id"], item["state"], item["volume"], tuple(item["group_members"])
            )
            for item in players
        ),
    )
    if (
        snapshot.run_id != expected_run_id
        or not _is_uuid(snapshot.run_id)
        or not _is_nonce(snapshot.nonce)
    ):
        raise RecoveryError("snapshot schema is unknown")
    targets = tuple(player.entity_id for player in snapshot.players)
    _assert_targets(targets)
    _assert_target_groups(snapshot.players, targets)
    return snapshot


def _assert_safe_root(root: Path) -> None:
    """Create or validate the private, owner-only recovery directory."""
    parent = root.parent
    if parent.is_symlink() or not parent.is_dir():
        raise RecoveryError("recovery root parent is unsafe")
    if root.is_symlink():
        raise RecoveryError("recovery root must not be a symlink")
    root.mkdir(mode=0o700, exist_ok=True)
    _assert_regular_dir(root)


def _assert_regular_dir(path: Path) -> None:
    """Reject redirected directories and directories accessible by another user."""
    if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.getuid():
        raise RecoveryError("recovery directory is unsafe")
    if path.stat().st_mode & 0o077:
        raise RecoveryError("recovery directory permissions are unsafe")


def _assert_regular_file(path: Path) -> None:
    """Require a private regular file owned by this process's user."""
    if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid():
        raise RecoveryError("recovery file is unsafe")
    if path.stat().st_mode & 0o077:
        raise RecoveryError("recovery file permissions are unsafe")


def _write_json_exclusive(path: Path, value: dict[str, Any]) -> None:
    """Write new private state without replacing an existing run artifact."""
    payload = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as err:
        raise RecoveryError("could not create private recovery state") from err
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
        raise RecoveryError("watchdog state is malformed") from err
    if (
        not isinstance(value, dict)
        or set(value) != {"run_id", "nonce", "deadline"}
        or value["run_id"] != snapshot.run_id
        or value["nonce"] != snapshot.nonce
        or type(value["deadline"]) not in (int, float)
        or not math.isfinite(value["deadline"])
        or value["deadline"] > time.time() + MAX_RUN_AGE
    ):
        raise RecoveryError("watchdog does not own this recovery run")
    return value


@contextmanager
def _mutation_lock(root: Path, deadline: float) -> Generator[None]:
    """Serialize mutations while consuming only the caller's absolute recovery budget."""
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
    """Keep one application-wide lease inherited by the detached recovery child."""
    descriptor = os.open(
        root / "watchdog.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
    )
    try:
        _assert_private_lease(descriptor)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as err:
            raise RecoveryError("an application recovery watchdog is already armed") from err
    except (OSError, RecoveryError):
        os.close(descriptor)
        raise
    return descriptor


def _assert_private_lease(descriptor: int) -> None:
    """Validate the opened lease itself rather than a racy pathname lookup."""
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RecoveryError("watchdog lease is unsafe")


def _has_verified_receipt(run_dir: Path, snapshot: Snapshot) -> bool:
    """Skip a settled run without letting an invalid receipt suppress recovery."""
    path = run_dir / "receipt.json"
    if not path.exists():
        return False
    try:
        _assert_regular_file(path)
        if path.stat().st_size > MAX_RESPONSE:
            return False
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecoveryError):
        return False
    return (
        isinstance(receipt, dict)
        and receipt.get("verified") is True
        and receipt
        in (
            {
                "run_id": snapshot.run_id,
                "nonce": snapshot.nonce,
                "verified": True,
                "status": "restoration verified",
            },
            {
                "run_id": snapshot.run_id,
                "nonce": snapshot.nonce,
                "verified": True,
                "status": "HA app/state restoration verified; native peers excluded",
            },
        )
    )


def _write_receipt(run_dir: Path, snapshot: Snapshot, verified: bool, status: str) -> None:
    """Record HA-only verification, preserving failed evidence before promotion."""
    path = run_dir / "receipt.json"
    receipt = {
        "run_id": snapshot.run_id,
        "nonce": snapshot.nonce,
        "verified": verified,
        "status": "HA app/state restoration verified; native peers excluded"
        if verified
        else status,
    }
    if path.exists():
        _assert_regular_file(path)
        if path.stat().st_size > MAX_RESPONSE:
            raise RecoveryError("recovery receipt exceeded limit")
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as err:
            raise RecoveryError("recovery receipt is malformed") from err
        if existing == receipt:
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
            raise RecoveryError("recovery receipt conflicts with this restore")
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
        raise RecoveryError("failed recovery evidence exceeded limit")
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise RecoveryError("failed recovery evidence is malformed") from err
    if existing != expected:
        raise RecoveryError("failed recovery evidence conflicts with this restore")


def _replace_json(path: Path, value: dict[str, Any]) -> None:
    """Atomically replace a validated receipt only inside its private run directory."""
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    _write_json_exclusive(temporary, value)
    try:
        temporary.replace(path)
    except OSError as err:
        temporary.unlink(missing_ok=True)
        raise RecoveryError("could not replace private recovery receipt") from err


def _require_deadline(deadline: float) -> None:
    """Refuse further work once the absolute recovery budget expires."""
    if time.monotonic() >= deadline:
        raise RecoveryError("recovery deadline expired")


def _is_uuid(value: object) -> bool:
    """Return whether an untrusted value is a canonical UUID string."""
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _assert_targets(targets: tuple[str, ...]) -> None:
    """Validate a canonical nonempty subset of the two fixed recovery targets."""
    if targets not in CAPTURE_TARGETS.values():
        raise RecoveryError("recovery targets are not an approved canonical subset")


def _is_nonce(value: str) -> bool:
    """Return whether the stored nonce is a full lowercase hexadecimal secret."""
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _require_run_id(run_id: str | None) -> str:
    """Return a required run identity for commands that operate on saved state."""
    if not run_id:
        raise RecoveryError("--run is required")
    return run_id


def _self_test_snapshot() -> Snapshot:
    """Create synthetic idle state without reading HAOS or a HomePod."""
    return Snapshot(
        str(uuid.uuid4()),
        secrets.token_hex(32),
        time.time(),
        True,
        True,
        "2.10.7",
        False,
        tuple(PlayerState(target, "idle", 0.5, ()) for target in TARGETS),
    )


def _simulation_transport(root: Path, run_id: str) -> FakeTransport:
    """Create fake child transport only for the exact armed self-test run."""
    run_dir, snapshot = load_snapshot(root, run_id)
    path = run_dir / "simulation.json"
    _assert_regular_file(path)
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise RecoveryError("simulation marker is malformed") from err
    if marker != {"run_id": snapshot.run_id, "nonce": snapshot.nonce}:
        raise RecoveryError("simulation marker does not own this recovery run")
    return FakeTransport()


class FakeTransport:
    """In-memory transport only for tests and the CLI's isolated self-test child."""

    def __init__(self) -> None:
        """Initialize only the two fixed fake players and add-ons."""
        self.official_started = True
        self.dev_started = True
        self.dev_installed = True
        self.players = {target: PlayerState(target, "idle", 0.5, ()) for target in TARGETS}

    def public_info(self) -> None:
        """Model the always-ready fixed local public endpoint."""

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        """Model the fixed calls made by recovery, without contacting HAOS."""
        if path == f"/addons/{OFFICIAL}/info":
            return {
                "installed": True,
                "state": "started" if self.official_started else "stopped",
                "protected": True,
                "version": OFFICIAL_VERSION,
            }
        if path == f"/addons/{DEV}/info":
            if not self.dev_installed:
                return {"installed": False, "state": "unknown", "protected": None, "version": None}
            return {
                "installed": True,
                "state": "started" if self.dev_started else "stopped",
                "protected": False,
                "version": "2.11.0.dev",
            }
        if path == f"/addons/{DEV}/stop":
            self.dev_started = False
            return None
        if path == f"/addons/{OFFICIAL}/start":
            self.official_started = True
            return None
        if path == "/core/api/template":
            return json.dumps(list(TARGETS))
        if path == "/core/api/services/homeassistant/reload_config_entry":
            return []
        if path.startswith("/core/api/states/"):
            player = self.players[path.rsplit("/", 1)[1]]
            return {
                "entity_id": player.entity_id,
                "state": player.state,
                "attributes": {
                    "volume_level": player.volume,
                    "group_members": list(player.group_members),
                },
            }
        if body is None:
            raise RecoveryError("self-test fake transport requires a request body")
        if path.endswith("/unjoin"):
            entity = body["entity_id"]
            self.players[entity] = PlayerState(
                entity, self.players[entity].state, self.players[entity].volume, ()
            )
            return None
        if path.endswith("/volume_set"):
            entity = body["entity_id"]
            current = self.players[entity]
            self.players[entity] = PlayerState(
                entity, current.state, body["volume_level"], current.group_members
            )
            return None
        if path.endswith("/media_stop"):
            entity = body["entity_id"]
            current = self.players[entity]
            self.players[entity] = PlayerState(
                entity, "idle", current.volume, current.group_members
            )
            return None
        if path.endswith("/media_pause"):
            entity = body["entity_id"]
            current = self.players[entity]
            self.players[entity] = PlayerState(
                entity, "paused", current.volume, current.group_members
            )
            return None
        if path.endswith("/turn_off"):
            entity = body["entity_id"]
            current = self.players[entity]
            self.players[entity] = PlayerState(entity, "off", current.volume, current.group_members)
            return None
        if path.endswith("/media_play"):
            entity = body["entity_id"]
            current = self.players[entity]
            self.players[entity] = PlayerState(
                entity, "playing", current.volume, current.group_members
            )
            return None
        raise RecoveryError("self-test fake transport rejected request")


def main(argv: list[str] | None = None) -> int:
    """Run one explicit private recovery operation."""
    parser = argparse.ArgumentParser(description="Private native AirPlay trial recovery controller")
    parser.add_argument(
        "command", choices=("capture", "restore", "arm", "watchdog", "self-test", "self-test-child")
    )
    parser.add_argument("--run")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--target", choices=tuple(CAPTURE_TARGETS), default="both")
    args = parser.parse_args(argv)
    try:
        if args.command == "self-test":
            self_test(args.root)
        elif args.command == "capture":
            snapshot = capture(
                SupervisorTransport(
                    os.environ.get("SUPERVISOR_TOKEN", ""), time.monotonic() + RESTORE_TIMEOUT
                ),
                CAPTURE_TARGETS[args.target],
            )
            sys.stdout.write(f"{save_snapshot(args.root, snapshot)}\n")
        else:
            run_id = _require_run_id(args.run)
            if args.command == "arm":
                sys.stdout.write(f"{arm_watchdog(args.root, run_id, args.timeout)}\n")
            elif args.command == "restore":
                run_dir, snapshot = load_snapshot(args.root, run_id)
                deadline = time.monotonic() + RESTORE_TIMEOUT
                with _mutation_lock(args.root, deadline):
                    if not _has_verified_receipt(run_dir, snapshot):
                        try:
                            restore(
                                SupervisorTransport(
                                    os.environ.get("SUPERVISOR_TOKEN", ""),
                                    deadline,
                                ),
                                snapshot,
                                deadline,
                            )
                        except RecoveryError as err:
                            _write_receipt(run_dir, snapshot, False, str(err))
                            raise
                        _write_receipt(run_dir, snapshot, True, "restoration verified")
            elif args.command == "self-test-child":
                watchdog(args.root, run_id, _simulation_transport(args.root, run_id))
            else:
                watchdog(args.root, run_id)
    except RecoveryError as err:
        LOGGER.error(f"Recovery failed: {err}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
