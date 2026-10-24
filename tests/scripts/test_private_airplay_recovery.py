"""Tests for the private native AirPlay trial recovery controller."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager, suppress
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Never

import pytest

from scripts import private_airplay_recovery as recovery


class RecordingTransport(recovery.FakeTransport):
    """Fake controller transport with controllable restore failures."""

    def __init__(self) -> None:
        """Initialize an otherwise successful fake transport."""
        super().__init__()
        self.calls: list[str] = []
        self.stop_works = True
        self.start_works = True
        self.available = True
        self.bad_verify = False
        self.bad_group = False
        self.response: Any | None = None

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        """Record the fixed endpoint then optionally model an unavailable response."""
        self.calls.append(path)
        if self.response is not None:
            return self.response
        if path == f"/addons/{recovery.DEV}/stop" and not self.stop_works:
            return None
        if path == f"/addons/{OFFICIAL}/start" and not self.start_works:
            return None
        if path == f"/addons/{recovery.DEV}/info" and not self.stop_works:
            return {
                "installed": True,
                "state": "started",
                "protected": False,
                "version": "2.11.0.dev",
            }
        if path == f"/addons/{OFFICIAL}/info" and not self.start_works:
            return {"installed": True, "state": "stopped", "protected": True, "version": "2.10.7"}
        if path.startswith("/core/api/states/") and not self.available:
            entity = path.rsplit("/", 1)[1]
            return {"entity_id": entity, "state": "unavailable", "attributes": {}}
        result = super().request(method, path, body)
        if path.startswith("/core/api/states/") and self.bad_verify:
            result["attributes"]["volume_level"] = 0.0
        if path.startswith("/core/api/states/") and self.bad_group:
            result["attributes"]["group_members"] = ["media_player.mainpod"]
        return result


class DevStateTransport(RecordingTransport):
    """Return a fixed sequence of Supervisor DEV states through the real helper."""

    def __init__(self, states: list[Any]) -> None:
        """Keep the final state stable after consuming earlier observations."""
        super().__init__()
        self.states = list(states)

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        """Override only DEV info; normal stop calls still return successfully."""
        if path == f"/addons/{recovery.DEV}/info":
            self.calls.append(path)
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            return {"installed": True, "state": state, "protected": True, "version": "2.11.dev"}
        return super().request(method, path, body)


UNSAFE_DEV_STATES: list[Any] = ["error", "stopping", "starting", "unknown", "", None, 1, [], {}]
OFFICIAL = recovery.OFFICIAL


@contextmanager
def _fixed_http_server(
    responses: dict[str, tuple[int, bytes, float, float]],
) -> Generator[tuple[str, list[tuple[str, str, dict[str, str], bytes]]]]:
    """Serve fixed local curl fixtures with optional header and body delays."""
    calls: list[tuple[str, str, dict[str, str], bytes]] = []

    class Handler(BaseHTTPRequestHandler):
        """Return one configured response without writing fixture logs."""

        def _respond(self) -> None:
            """Record one request and emit the configured response in controlled phases."""
            length = int(self.headers.get("Content-Length", "0"))
            calls.append((self.command, self.path, dict(self.headers), self.rfile.read(length)))
            status, body, header_delay, body_delay = responses[self.path]
            time.sleep(header_delay)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            time.sleep(body_delay)
            with suppress(BrokenPipeError):
                self.wfile.write(body)

        def __getattr__(self, name: str) -> Any:
            """Map only HTTP verb dispatch to the one fixed fixture responder."""
            if name in {"do_GET", "do_POST"}:
                return self._respond
            raise AttributeError(name)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def snapshot() -> recovery.Snapshot:
    """Return the approved idle snapshot without any queue or media payload."""
    return recovery.Snapshot(
        "123e4567-e89b-12d3-a456-426614174000",
        "a" * 64,
        time.time(),
        True,
        True,
        "2.10.7",
        False,
        tuple(recovery.PlayerState(entity, "idle", 0.5, ()) for entity in recovery.TARGETS),
    )


class DelayedGroupTransport(RecordingTransport):
    """Accept group requests before the startup group path becomes effective."""

    def __init__(self, clock: list[float], ready_at: float) -> None:
        """Bind deterministic startup readiness without changing the recovery deadline."""
        super().__init__()
        self.clock = clock
        self.ready_at = ready_at
        self.official_started = False

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        """Apply only joins made after readiness, matching the asynchronous service seam."""
        if path == "/core/api/services/media_player/join":
            self.calls.append(path)
            assert method == "POST"
            assert body == {
                "entity_id": "media_player.officepod",
                "group_members": ["media_player.mainpod"],
            }
            if self.clock[0] >= self.ready_at:
                for entity, state in self.players.items():
                    self.players[entity] = replace(
                        state, group_members=("media_player.officepod", "media_player.mainpod")
                    )
            return None
        return super().request(method, path, body)


@pytest.mark.parametrize("ready_at", [1.0, 6.0])
def test_restore_reconciles_startup_group_within_original_deadline(
    monkeypatch: pytest.MonkeyPatch, ready_at: float
) -> None:
    """An ineffective first join must not strand a now-recoverable captured group."""
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    transport = DelayedGroupTransport(clock, ready_at)
    state = snapshot()
    state = replace(
        state,
        players=tuple(
            replace(player, group_members=("media_player.officepod", "media_player.mainpod"))
            for player in state.players
        ),
    )
    recovery.restore(transport, state, 20.0)
    assert all(
        recovery._matches(expected, transport.players[expected.entity_id])
        for expected in state.players
    )
    assert transport.calls.count("/core/api/services/media_player/join") >= 2
    assert clock[0] < 20.0


def test_restore_never_extends_deadline_for_missing_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """A group unavailable throughout the original budget still fails closed."""
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    transport = DelayedGroupTransport(clock, 1000.0)
    state = snapshot()
    state = replace(
        state,
        players=tuple(
            replace(player, group_members=("media_player.officepod", "media_player.mainpod"))
            for player in state.players
        ),
    )
    with pytest.raises(recovery.RecoveryError, match="recovery deadline expired"):
        recovery.restore(transport, state, 12.0)
    assert clock[0] == 12.0
    assert all(not player.group_members for player in transport.players.values())


def test_restore_refuses_new_uncaptured_group_before_unjoin() -> None:
    """Restoration must not mutate a newly grouped player outside the captured scope."""
    transport = RecordingTransport()
    entity = "media_player.mainpod"
    transport.players[entity] = replace(
        transport.players[entity], group_members=(entity, "media_player.uncaptured")
    )
    with pytest.raises(recovery.RecoveryError, match="not exclusively recoverable"):
        recovery.restore(transport, snapshot(), time.monotonic() + 1)
    assert "/core/api/services/media_player/unjoin" not in transport.calls


def test_verify_never_turns_volume_telemetry_into_group_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A volume-only mismatch must not authorize retries or receiver volume commands."""
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    transport = RecordingTransport()
    transport.bad_verify = True
    with pytest.raises(recovery.RecoveryError, match="recovery deadline expired"):
        recovery._verify(transport, snapshot().players, 12.0)
    assert all("/services/" not in path for path in transport.calls)
    assert clock[0] == 12.0


def test_verify_rejects_matching_state_read_after_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even a matching response cannot promote recovery after the absolute deadline."""
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    class SlowReadTransport(RecordingTransport):
        """Consume the shared budget while retrieving otherwise matching state."""

        def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
            """Advance the fake clock only at the real HA state-read seam."""
            if path.startswith("/core/api/states/"):
                clock[0] += 1.0
            return super().request(method, path, body)

    with pytest.raises(recovery.RecoveryError, match="recovery deadline expired"):
        recovery._verify(SlowReadTransport(), snapshot().players, 1.0)


def test_restore_stops_dev_before_official_and_is_idempotent() -> None:
    """Restoration stops DEV first and safely repeats an already restored state."""
    transport = RecordingTransport()
    state = snapshot()
    recovery.restore(transport, state, time.monotonic() + 1)
    assert transport.calls.index(f"/addons/{recovery.DEV}/stop") < transport.calls.index(
        f"/addons/{OFFICIAL}/info"
    )
    transport.calls.clear()
    recovery.restore(transport, state, time.monotonic() + 1)
    assert f"/addons/{recovery.DEV}/stop" not in transport.calls


def test_restore_fails_closed_when_dev_cannot_stop() -> None:
    """An active DEV add-on prevents every later restoration operation."""
    transport = RecordingTransport()
    transport.stop_works = False
    with pytest.raises(recovery.RecoveryError, match="DEV add-on did not stop"):
        recovery.restore(transport, snapshot(), time.monotonic() + 1)
    assert f"/addons/{OFFICIAL}/start" not in transport.calls


@pytest.mark.parametrize("state", UNSAFE_DEV_STATES)
def test_capture_rejects_unconfirmed_dev_stop(state: Any) -> None:
    """Only exact stopped DEV state permits a new recovery snapshot."""
    transport = DevStateTransport([state])
    with pytest.raises(recovery.RecoveryError):
        recovery.capture(transport, recovery.CAPTURE_TARGETS["main"])
    assert all("/core/" not in path and not path.endswith("/stop") for path in transport.calls)


@pytest.mark.parametrize("state", UNSAFE_DEV_STATES)
@pytest.mark.parametrize("after_stop", [False, True], ids=["initial", "after-stop"])
def test_watchdog_never_verifies_unconfirmed_dev_stop(
    tmp_path: Path, state: Any, after_stop: bool
) -> None:
    """Unknown DEV state cannot pass restore or write a verified receipt, even after stop."""
    transport = DevStateTransport(["started", state] if after_stop else [state])
    saved = replace(snapshot(), players=(snapshot().players[0],))
    run_dir = recovery.save_snapshot(tmp_path, saved)
    recovery._write_json_exclusive(
        run_dir / "watchdog.json",
        {"run_id": saved.run_id, "nonce": saved.nonce, "deadline": time.time()},
    )
    with pytest.raises(recovery.RecoveryError):
        recovery.watchdog(tmp_path, saved.run_id, transport)
    receipt = json.loads((run_dir / "receipt.json").read_text())
    assert receipt["verified"] is False
    assert (f"/addons/{recovery.DEV}/stop" in transport.calls) is after_stop
    assert all(
        f"/addons/{OFFICIAL}/" not in path and "/core/" not in path for path in transport.calls
    )


@pytest.mark.parametrize("operation", ["capture", "restore"])
def test_uninstalled_catalog_is_not_a_confirmed_dev_stop(operation: str) -> None:
    """A valid catalog shape with unknown state is not safe stopped evidence."""
    transport = RecordingTransport()
    transport.dev_installed = False
    if operation == "capture":
        with pytest.raises(recovery.RecoveryError):
            recovery.capture(transport, recovery.CAPTURE_TARGETS["main"])
    else:
        saved = replace(snapshot(), players=(snapshot().players[0],))
        with pytest.raises(recovery.RecoveryError):
            recovery.restore(transport, saved, time.monotonic() + 1)
    assert f"/addons/{OFFICIAL}/start" not in transport.calls
    assert f"/addons/{recovery.DEV}/stop" not in transport.calls
    assert all("/core/" not in path for path in transport.calls)


@pytest.mark.parametrize("state", UNSAFE_DEV_STATES)
def test_restore_rejects_uncertain_official_state_before_start(state: Any) -> None:
    """The same predicate must not treat uncertain official state as safe to start."""

    class UncertainOfficialTransport(RecordingTransport):
        """Expose uncertain official state without changing the DEV stop path."""

        def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
            """Override only official app info and record the observation."""
            if path == f"/addons/{OFFICIAL}/info":
                self.calls.append(path)
                return {"state": state, "protected": True, "version": "2.10.7"}
            return super().request(method, path, body)

    transport = UncertainOfficialTransport()
    saved = replace(snapshot(), players=(snapshot().players[0],))
    with pytest.raises(recovery.RecoveryError):
        recovery.restore(transport, saved, time.monotonic() + 1)
    assert f"/addons/{OFFICIAL}/start" not in transport.calls
    assert all("/core/" not in path for path in transport.calls)


def test_restore_fails_when_official_cannot_start() -> None:
    """A stopped official app cannot be claimed restored when its start fails."""
    transport = RecordingTransport()
    transport.official_started = False
    transport.start_works = False
    with pytest.raises(recovery.RecoveryError, match="official add-on did not start"):
        recovery.restore(transport, snapshot(), time.monotonic() + 1)


def test_restore_fails_when_target_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unavailable targets consume only the bounded recovery deadline."""
    transport = RecordingTransport()
    transport.available = False
    monkeypatch.setattr(time, "sleep", lambda _: None)
    with pytest.raises(recovery.RecoveryError, match="deadline"):
        recovery.restore(transport, snapshot(), time.monotonic() - 0.01)


def test_restore_fails_closed_on_changed_volume_before_mutating_it() -> None:
    """A changed stored level is never forced back onto the native receiver."""
    transport = RecordingTransport()
    transport.bad_verify = True
    with pytest.raises(recovery.RecoveryError, match="volume differs"):
        recovery.restore(transport, snapshot(), time.monotonic() + 1)


def test_restore_fails_on_group_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    """Malformed observed membership must fail before any group mutation."""
    transport = RecordingTransport()
    transport.bad_group = True
    monkeypatch.setattr(time, "sleep", lambda _: None)
    with pytest.raises(recovery.RecoveryError, match="not exclusively recoverable"):
        recovery.restore(transport, snapshot(), time.monotonic() + 0.01)
    assert "/core/api/services/media_player/unjoin" not in transport.calls


def test_capture_rejects_wrong_protected_version() -> None:
    """Capture requires the exact approved installed official version."""

    class WrongVersionTransport(RecordingTransport):
        def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
            if path == f"/addons/{OFFICIAL}/info":
                return {
                    "installed": True,
                    "state": "started",
                    "protected": True,
                    "version": "2.10.7.1",
                }
            return super().request(method, path, body)

    with pytest.raises(recovery.RecoveryError, match="approved protected version"):
        recovery.capture(WrongVersionTransport())


def test_capture_rejects_another_active_ma_stream() -> None:
    """Preflight refuses a disruptive window while another MA player streams."""

    class BusyTransport(RecordingTransport):
        def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
            if path == "/core/api/template":
                return json.dumps([*recovery.TARGETS, "media_player.other"])
            if path == "/core/api/states/media_player.other":
                return {"entity_id": "media_player.other", "state": "playing", "attributes": {}}
            return super().request(method, path, body)

    transport = BusyTransport()
    transport.dev_started = False
    with pytest.raises(recovery.RecoveryError, match="another Music Assistant stream"):
        recovery.capture(transport)


def test_load_rejects_symlink_and_unknown_snapshot(tmp_path: Path) -> None:
    """Snapshots cannot be redirected or silently interpreted as another schema."""
    run_dir = recovery.save_snapshot(tmp_path, snapshot())
    state = run_dir / "snapshot.json"
    state.unlink()
    state.symlink_to(tmp_path / "outside")
    with pytest.raises(recovery.RecoveryError, match="unsafe"):
        recovery.load_snapshot(tmp_path, snapshot().run_id)
    state.unlink()
    state.write_text('{"schema":999,"snapshot":{}}')
    state.chmod(0o600)
    with pytest.raises(recovery.RecoveryError, match="unknown"):
        recovery.load_snapshot(tmp_path, snapshot().run_id)


def test_arm_rejects_completed_or_redirected_receipt(tmp_path: Path) -> None:
    """A settled run or a broken receipt symlink cannot arm another watchdog."""
    state = snapshot()
    run_dir = recovery.save_snapshot(tmp_path, state)
    recovery._write_receipt(run_dir, state, True, "restoration verified")
    with pytest.raises(recovery.RecoveryError, match="fresh run"):
        recovery.arm_watchdog(tmp_path, state.run_id, 30, self_test=True)
    (run_dir / "receipt.json").unlink()
    (run_dir / "receipt.json").symlink_to(tmp_path / "missing-receipt")
    with pytest.raises(recovery.RecoveryError, match="fresh run"):
        recovery.arm_watchdog(tmp_path, state.run_id, 30, self_test=True)


def test_arm_rejects_stale_snapshot(tmp_path: Path) -> None:
    """A fresh run identity cannot arm a new trial outside the short window."""
    stale = recovery.Snapshot(
        snapshot().run_id,
        snapshot().nonce,
        time.time() - recovery.MAX_RUN_AGE - 1,
        True,
        True,
        recovery.OFFICIAL_VERSION,
        False,
        snapshot().players,
    )
    recovery.save_snapshot(tmp_path, stale)
    with pytest.raises(recovery.RecoveryError, match="too old"):
        recovery.arm_watchdog(tmp_path, stale.run_id, 30, self_test=True)


def test_load_rejects_oversized_snapshot(tmp_path: Path) -> None:
    """An oversized state file cannot cause unbounded parsing."""
    run_dir = recovery.save_snapshot(tmp_path, snapshot())
    state = run_dir / "snapshot.json"
    state.write_bytes(b"x" * (recovery.MAX_RESPONSE + 1))
    state.chmod(0o600)
    with pytest.raises(recovery.RecoveryError, match="exceeded limit"):
        recovery.load_snapshot(tmp_path, snapshot().run_id)


def test_capture_rejects_malformed_response() -> None:
    """Malformed Supervisor data fails closed before snapshot creation."""
    transport = RecordingTransport()
    transport.response = []
    with pytest.raises(recovery.RecoveryError, match="malformed"):
        recovery.capture(transport)


def test_watchdog_rejects_wrong_owner_and_writes_failure_receipt(tmp_path: Path) -> None:
    """A child cannot restore a run armed with a different nonce."""
    run_dir = recovery.save_snapshot(tmp_path, snapshot())
    (run_dir / "watchdog.json").write_text(
        json.dumps({"run_id": snapshot().run_id, "nonce": "b" * 64, "deadline": time.time()})
    )
    (run_dir / "watchdog.json").chmod(0o600)
    with pytest.raises(recovery.RecoveryError, match="does not own"):
        recovery.watchdog(tmp_path, snapshot().run_id, RecordingTransport())


def test_self_test_detaches_and_verifies_receipt(tmp_path: Path) -> None:
    """The CLI's fake-only watchdog path proves child identity and receipt handling."""
    recovery.self_test(tmp_path)
    receipts = list(tmp_path.glob("*/receipt.json"))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_text())["verified"] is True


def test_supervisor_transport_real_wire_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """Native curl preserves Supervisor envelopes and sends auth only in stdin config."""
    addon = json.dumps(
        {
            "result": "ok",
            "data": {
                "installed": True,
                "state": "started",
                "protected": True,
                "version": "2.10.7",
            },
        }
    ).encode()
    dev = json.dumps(
        {
            "result": "ok",
            "data": {
                "installed": True,
                "state": "stopped",
                "protected": True,
                "version": "2.11.0.dev",
            },
        }
    ).encode()
    target = json.dumps(
        {
            "entity_id": "media_player.mainpod",
            "state": "idle",
            "attributes": {"volume_level": 0.5, "group_members": []},
        }
    ).encode()
    with _fixed_http_server(
        {
            f"/addons/{OFFICIAL}/info": (200, addon, 0, 0),
            f"/addons/{recovery.DEV}/info": (200, dev, 0, 0),
            "/core/api/template": (200, json.dumps(["media_player.mainpod"]).encode(), 0, 0),
            "/core/api/states/media_player.mainpod": (200, target, 0, 0),
        }
    ) as (base_url, calls):
        monkeypatch.setattr(recovery, "SUPERVISOR_BASE_URL", base_url)
        state = recovery.capture(
            recovery.SupervisorTransport("private-token", time.monotonic() + 2),
            recovery.CAPTURE_TARGETS["main"],
        )
    assert state.players[0].entity_id == "media_player.mainpod"
    assert all(call[2]["Authorization"] == "Bearer private-token" for call in calls)
    assert all("private-token" not in call[1] for call in calls)
    payloads = {path: json.loads(body) for _, path, _, body in calls if body}
    assert payloads["/core/api/template"] == {
        "template": "{{ integration_entities('music_assistant') | tojson }}"
    }


def test_supervisor_transport_rejects_unsuccessful_http_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful-looking body cannot mask a non-success HTTP action result."""
    path = f"/addons/{OFFICIAL}/info"
    with _fixed_http_server({path: (500, b'{"result":"ok","data":{}}', 0, 0)}) as (base_url, _):
        monkeypatch.setattr(recovery, "SUPERVISOR_BASE_URL", base_url)
        with pytest.raises(recovery.RecoveryError, match="HTTP status"):
            recovery.SupervisorTransport("token", time.monotonic() + 1).request("GET", path)


def test_supervisor_transport_bounds_oversized_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """Native curl rejects a response larger than its fixed output bound."""
    path = f"/addons/{OFFICIAL}/info"
    with _fixed_http_server({path: (200, b"x" * (recovery.MAX_RESPONSE + 1), 0, 0)}) as (
        base_url,
        _,
    ):
        monkeypatch.setattr(recovery, "SUPERVISOR_BASE_URL", base_url)
        with pytest.raises(recovery.RecoveryError, match="exceeded limit"):
            recovery.SupervisorTransport("token", time.monotonic() + 1).request("GET", path)


@pytest.mark.parametrize(("header_delay", "body_delay"), [(0.15, 0), (0, 0.15)])
def test_supervisor_transport_enforces_absolute_deadline_during_http_phases(
    monkeypatch: pytest.MonkeyPatch, header_delay: float, body_delay: float
) -> None:
    """Slow headers and trickled bodies cannot outlive the one shared curl deadline."""
    path = f"/addons/{OFFICIAL}/info"
    with _fixed_http_server({path: (200, b"{}", header_delay, body_delay)}) as (base_url, _):
        monkeypatch.setattr(recovery, "SUPERVISOR_BASE_URL", base_url)
        started = time.monotonic()
        with pytest.raises(recovery.RecoveryError, match="HTTP request failed"):
            recovery.SupervisorTransport("token", started + 0.05).request("GET", path)
        assert time.monotonic() - started < 0.14


def test_capture_accepts_observed_app_shapes_and_main_scope() -> None:
    """Live missing-`installed` app fields and direct template lists support main-only capture."""

    class LiveShapeTransport(RecordingTransport):
        def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
            if path == f"/addons/{OFFICIAL}/info":
                return {"state": "started", "protected": True, "version": "2.10.7"}
            if path == f"/addons/{recovery.DEV}/info":
                return {"state": "stopped", "protected": True, "version": "2.11.0.dev2026102103"}
            if path == "/core/api/template":
                return ["media_player.mainpod", "media_player.officepod", "media_player.other"]
            if path in {
                "/core/api/states/media_player.officepod",
                "/core/api/states/media_player.other",
            }:
                entity = path.rsplit("/", 1)[1]
                return {"entity_id": entity, "state": "unavailable", "attributes": {}}
            return super().request(method, path, body)

    snapshot = recovery.capture(LiveShapeTransport(), recovery.CAPTURE_TARGETS["main"])
    assert tuple(player.entity_id for player in snapshot.players) == ("media_player.mainpod",)


def test_wait_retries_transient_target_unavailability(monkeypatch: pytest.MonkeyPatch) -> None:
    """Availability polling accepts transient HA states until the bounded deadline."""

    class EventuallyAvailable(RecordingTransport):
        reads = 0

        def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
            if path == "/core/api/states/media_player.mainpod":
                self.reads += 1
                state = "unavailable" if self.reads == 1 else "idle"
                return {"entity_id": "media_player.mainpod", "state": state, "attributes": {}}
            return super().request(method, path, body)

    monkeypatch.setattr(time, "sleep", lambda _: None)
    recovery._wait_for_targets(
        EventuallyAvailable(),
        (recovery.PlayerState("media_player.mainpod", "idle", None, ()),),
        time.monotonic() + 1,
    )


def test_single_target_snapshot_round_trips_and_invalid_subsets_fail(tmp_path: Path) -> None:
    """Saved state permits canonical one-target recovery and rejects invented subsets."""
    transport = recovery.FakeTransport()
    transport.dev_started = False
    main = recovery.capture(transport, recovery.CAPTURE_TARGETS["main"])
    recovery.save_snapshot(tmp_path, main)
    _, loaded = recovery.load_snapshot(tmp_path, main.run_id)
    assert loaded.players == main.players
    recovery.restore(transport, loaded, time.monotonic() + 1)
    with pytest.raises(recovery.RecoveryError, match="canonical subset"):
        recovery._assert_targets(("media_player.officepod", "media_player.mainpod"))


def test_watchdog_rejects_overlapping_application_owners(tmp_path: Path) -> None:
    """Two run IDs cannot concurrently own the same DEV/official app restoration."""
    first = snapshot()
    second = replace(first, run_id="223e4567-e89b-12d3-a456-426614174000")
    recovery.save_snapshot(tmp_path, first)
    recovery.save_snapshot(tmp_path, second)
    children = [recovery.arm_watchdog(tmp_path, first.run_id, 0.3, self_test=True)]
    try:
        with pytest.raises(recovery.RecoveryError, match="already armed"):
            children.append(recovery.arm_watchdog(tmp_path, second.run_id, 0.3, self_test=True))
    finally:
        for child in children:
            os.waitpid(child, 0)


def test_completed_restore_prevents_delayed_watchdog_mutation(tmp_path: Path) -> None:
    """A settled run cannot later stop DEV during the next approved experiment."""
    state = snapshot()
    run_dir = recovery.save_snapshot(tmp_path, state)
    recovery._write_json_exclusive(
        run_dir / "watchdog.json",
        {"run_id": state.run_id, "nonce": state.nonce, "deadline": time.time()},
    )
    recovery._write_receipt(run_dir, state, True, "restoration verified")
    transport = RecordingTransport()
    recovery.watchdog(tmp_path, state.run_id, transport)
    assert transport.calls == []


def test_restore_refreshes_existing_integration_after_app_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reset the measured HA reconnect backoff without reconfiguring the entry."""

    class DelayedIntegration(RecordingTransport):
        needs_refresh = False

        def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
            if path == "/core/api/services/homeassistant/reload_config_entry":
                assert body == {"entity_id": "media_player.mainpod"}
                self.needs_refresh = False
                return []
            if path.startswith("/core/api/states/") and self.needs_refresh:
                return {
                    "entity_id": path.rsplit("/", 1)[1],
                    "state": "unavailable",
                    "attributes": {},
                }
            result = super().request(method, path, body)
            if path == f"/addons/{OFFICIAL}/start":
                self.needs_refresh = True
            return result

    transport = DelayedIntegration()
    transport.official_started = False
    state = replace(snapshot(), players=(snapshot().players[0],))
    monkeypatch.setattr(time, "sleep", lambda _: None)
    recovery.restore(transport, state, time.monotonic() + 0.1)
    assert transport.needs_refresh is False


def test_public_readiness_retries_only_transient_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """A scoped readiness retry preserves the existing absolute recovery deadline."""

    class EventuallyReady(RecordingTransport):
        attempts = 0

        def public_info(self) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise recovery.TransientAvailabilityError("temporary")

    monkeypatch.setattr(time, "sleep", lambda _: None)
    transport = EventuallyReady()
    recovery._wait_for_public_api(transport, time.monotonic() + 1)
    assert transport.attempts == 2


def test_restore_skips_noop_player_service_calls() -> None:
    """An already restored HA state never changes receiver-facing player services."""
    transport = RecordingTransport()
    recovery.restore(transport, snapshot(), time.monotonic() + 1)
    assert not any("/core/api/services/media_player/" in call for call in transport.calls)


def test_restore_fails_closed_instead_of_resetting_changed_ha_volume() -> None:
    """A changed stored HA level is evidence to preserve, not a receiver volume target."""
    transport = RecordingTransport()
    player = transport.players["media_player.mainpod"]
    transport.players[player.entity_id] = recovery.PlayerState(player.entity_id, "idle", 0.7, ())
    with pytest.raises(recovery.RecoveryError, match="volume differs"):
        recovery.restore(transport, snapshot(), time.monotonic() + 1)
    assert "/core/api/services/media_player/volume_set" not in transport.calls


def test_failed_receipt_can_be_replaced_only_after_verified_own_restore(tmp_path: Path) -> None:
    """A prior failed attempt remains evidence until its own run verifies a later restore."""
    state = snapshot()
    run_dir = recovery.save_snapshot(tmp_path, state)
    recovery._write_receipt(run_dir, state, False, "recovery deadline expired")
    recovery._write_receipt(run_dir, state, True, "restoration verified")
    assert recovery._has_verified_receipt(run_dir, state)
    assert json.loads((run_dir / "failed_receipt.json").read_text()) == {
        "run_id": state.run_id,
        "nonce": state.nonce,
        "verified": False,
        "status": "recovery deadline expired",
    }


def test_failed_receipt_rejects_foreign_nonce_upgrade(tmp_path: Path) -> None:
    """A receipt from another run cannot become a verified result."""
    state = snapshot()
    run_dir = recovery.save_snapshot(tmp_path, state)
    recovery._write_json_exclusive(
        run_dir / "receipt.json",
        {"run_id": state.run_id, "nonce": "b" * 64, "verified": False, "status": "failed"},
    )
    with pytest.raises(recovery.RecoveryError, match="conflicts"):
        recovery._write_receipt(run_dir, state, True, "restoration verified")


def test_mutation_lock_exhausts_its_absolute_deadline(tmp_path: Path) -> None:
    """A blocked recovery reports its exhausted bounded mutation budget."""
    lock = recovery._mutation_lock(tmp_path, time.monotonic() + 1)
    with (
        lock,
        pytest.raises(recovery.RecoveryError, match="deadline"),
        recovery._mutation_lock(tmp_path, time.monotonic() - 0.01),
    ):
        pytest.fail("expired lock must not be acquired")


def test_self_test_child_requires_matching_simulation_marker(tmp_path: Path) -> None:
    """The CLI fake transport cannot be selected for an ordinary saved recovery run."""
    state = snapshot()
    recovery.save_snapshot(tmp_path, state)
    assert recovery.main(["self-test-child", "--root", str(tmp_path), "--run", state.run_id]) == 1


def test_watchdog_does_not_run_python_after_fork(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Detached launch must exec cleanly even when the caller has other threads."""

    def unsafe_fork() -> Never:
        pytest.fail("Python-after-fork is unsafe in a multithreaded caller")

    monkeypatch.setattr(os, "fork", unsafe_fork)
    state = snapshot()
    recovery.save_snapshot(tmp_path, state)
    child = recovery.arm_watchdog(tmp_path, state.run_id, 0.05, self_test=True)
    _, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 0


def test_failed_receipt_promotion_resumes_after_archive_was_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash between evidence archival and atomic replacement remains recoverable."""
    state = snapshot()
    run_dir = recovery.save_snapshot(tmp_path, state)
    recovery._write_receipt(run_dir, state, False, "initial recovery failed")
    replace_json = recovery._replace_json

    def interrupted_replace(path: Path, value: dict[str, Any]) -> None:
        """Interrupt the verified receipt replacement after the archive is durable."""
        assert path == run_dir / "receipt.json"
        assert value["verified"] is True
        raise recovery.RecoveryError("interrupted before replacement")

    monkeypatch.setattr(recovery, "_replace_json", interrupted_replace)
    with pytest.raises(recovery.RecoveryError, match="interrupted"):
        recovery._write_receipt(run_dir, state, True, "restoration verified")
    assert (run_dir / "failed_receipt.json").exists()
    monkeypatch.setattr(recovery, "_replace_json", replace_json)
    recovery._write_receipt(run_dir, state, True, "restoration verified")
    assert recovery._has_verified_receipt(run_dir, state)
    assert json.loads((run_dir / "failed_receipt.json").read_text())["verified"] is False


@pytest.mark.parametrize(
    "body",
    [b"x" * (recovery.MAX_RESPONSE + 1), b"not-json", b'{"server_version":"other"}'],
    ids=("oversized", "malformed", "wrong-identity"),
)
def test_public_readiness_does_not_retry_protocol_or_identity_errors(
    body: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only network startup failures are transient, never invalid service identity."""
    with _fixed_http_server({"/info": (200, body, 0, 0)}) as (base, _):
        monkeypatch.setattr(recovery, "PUBLIC_INFO_URL", f"{base}/info")
        with pytest.raises(recovery.RecoveryError) as failure:
            recovery.SupervisorTransport("token", time.monotonic() + 2).public_info()
        assert not isinstance(failure.value, recovery.TransientAvailabilityError)
