"""Tests for the private stock-absent DEV-only lifecycle controller."""

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
from typing import Any

import pytest

from scripts import private_dev_lifecycle as lifecycle

DEV = lifecycle.DEV
STOCK = lifecycle.STOCK


class ScriptedTransport(lifecycle.FakeTransport):
    """Fake transport with overridable responses per fixed path."""

    def __init__(self, **kwargs: Any) -> None:
        """Start from the stock-absent fake and allow per-path overrides."""
        super().__init__(**kwargs)
        self.overrides: dict[str, Any] = {}
        self.stop_works = True

    def request(self, method: str, path: str) -> Any:
        """Serve an override verbatim, otherwise the fake, modelling a stuck stop."""
        if path in self.overrides:
            self.calls.append(f"{method} {path}")
            return self.overrides[path]
        if path == f"/addons/{DEV}/stop" and not self.stop_works:
            self.calls.append(f"{method} {path}")
            return None
        return super().request(method, path)


def snapshot(**changes: Any) -> lifecycle.Snapshot:
    """Return a snapshot matching the default fake catalog."""
    base = lifecycle.Snapshot(
        "123e4567-e89b-12d3-a456-426614174000",
        "a" * 64,
        time.time(),
        ("core_ssh", DEV),
        "2.11.0.dev",
    )
    return replace(base, **changes)


@contextmanager
def _fixed_http_server(
    responses: dict[str, tuple[int, bytes]],
) -> Generator[tuple[str, list[tuple[str, str, dict[str, str]]]]]:
    """Serve fixed local curl fixtures and record every request."""
    calls: list[tuple[str, str, dict[str, str]]] = []

    class Handler(BaseHTTPRequestHandler):
        def _respond(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            calls.append((self.command, self.path, dict(self.headers)))
            status, body = responses[self.path]
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            with suppress(BrokenPipeError):
                self.wfile.write(body)

        def __getattr__(self, name: str) -> Any:
            if name in {"do_GET", "do_POST"}:
                return self._respond
            raise AttributeError(name)

        def log_message(self, *_args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


# --- Facts: stock absence from the complete catalog -------------------------------------


def test_facts_come_from_the_complete_installed_catalog() -> None:
    """Absence is read from `GET /addons`; DEV facts from its own info."""
    transport = ScriptedTransport()
    facts = lifecycle.read_facts(transport)
    assert facts == lifecycle.Facts(("core_ssh", DEV), "stopped", "2.11.0.dev")
    assert transport.calls == ["GET /addons", f"GET /addons/{DEV}/info"]


def test_facts_reject_stock_present_or_dev_absent() -> None:
    """Stock in the catalog, or DEV missing, is not the stock-absent state."""
    with pytest.raises(lifecycle.LifecycleError, match="stock Music Assistant is installed"):
        lifecycle.read_facts(ScriptedTransport(slugs=(DEV, STOCK)))
    with pytest.raises(lifecycle.LifecycleError, match="DEV app is not installed"):
        lifecycle.read_facts(ScriptedTransport(slugs=("core_ssh",)))
    with pytest.raises(lifecycle.LifecycleError, match="unexpected Music Assistant"):
        lifecycle.read_facts(ScriptedTransport(slugs=(DEV, "local_music_assistant")))


@pytest.mark.parametrize(
    "catalog",
    [
        None,
        [],
        {},
        {"addons": []},
        {"addons": [{"slug": DEV, "state": "stopped", "version": "1"}], "extra": 1},
        {"addons": [{"slug": DEV, "state": "stopped"}]},
        {"addons": [{"slug": DEV, "version": "1"}]},
        {"addons": [{"slug": "", "state": "stopped", "version": "1"}]},
        {"addons": [{"slug": DEV, "state": "stopped", "version": "1"}, "x"]},
        {
            "addons": [
                {"slug": DEV, "state": "stopped", "version": "1"},
                {"slug": DEV, "state": "stopped", "version": "1"},
            ]
        },
        # an uninstalled-app info fallback shape must never be accepted as a catalog
        {"installed": False, "state": "unknown", "protected": None, "version": None},
    ],
)
def test_facts_reject_incomplete_catalog(catalog: Any) -> None:
    """A catalog that is not a validated complete list proves nothing about absence."""
    transport = ScriptedTransport()
    transport.overrides["/addons"] = catalog
    with pytest.raises(lifecycle.LifecycleError, match="catalog"):
        lifecycle.read_facts(transport)
    assert transport.calls == ["GET /addons"]


@pytest.mark.parametrize("state", ["error", "startup", "stopping", "unknown", "", None, 1])
def test_facts_reject_unconfirmed_dev_state(state: Any) -> None:
    """Only exact `started`/`stopped` are confirmed DEV states."""
    transport = ScriptedTransport()
    transport.overrides[f"/addons/{DEV}/info"] = {
        "slug": DEV,
        "state": state,
        "version": "2.11.0.dev",
        "protected": True,
        "boot": "manual",
        "watchdog": False,
    }
    with pytest.raises(lifecycle.LifecycleError, match=r"not confirmed|state is malformed"):
        lifecycle.read_facts(transport)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("protected", False, "protection mode is off"),
        ("protected", None, "protection mode is off"),
        ("boot", "auto", "boot mode is not manual"),
        ("boot", None, "boot mode is not manual"),
        ("watchdog", True, "Supervisor watchdog is on"),
        ("watchdog", None, "Supervisor watchdog is on"),
        ("slug", STOCK, "malformed"),
        ("version", "", "version is missing"),
    ],
)
def test_facts_require_protected_manual_unwatched_dev(field: str, value: Any, message: str) -> None:
    """DEV must stay protected, manual-boot and without Supervisor watchdog."""
    transport = ScriptedTransport()
    info = {
        "slug": DEV,
        "state": "stopped",
        "version": "2.11.0.dev",
        "protected": True,
        "boot": "manual",
        "watchdog": False,
        field: value,
    }
    transport.overrides[f"/addons/{DEV}/info"] = info
    with pytest.raises(lifecycle.LifecycleError, match=message):
        lifecycle.read_facts(transport)


# --- Capture / start / restore ----------------------------------------------------------


def test_capture_requires_dev_stopped_and_records_catalog() -> None:
    """A run is captured only from a stopped DEV and remembers the exact catalog."""
    with pytest.raises(lifecycle.LifecycleError, match="requires DEV stopped"):
        lifecycle.capture(ScriptedTransport(dev_started=True))
    state = lifecycle.capture(ScriptedTransport())
    assert state.installed_slugs == ("core_ssh", DEV)
    assert state.dev_version == "2.11.0.dev"
    assert lifecycle._is_uuid(state.run_id)
    assert lifecycle._is_nonce(state.nonce)


def test_start_only_from_matching_stopped_facts() -> None:
    """Start refuses drifted catalogs, drifted images and an already running DEV."""
    transport = ScriptedTransport()
    lifecycle.start(transport, snapshot())
    assert transport.dev_started is True
    assert f"POST /addons/{DEV}/start" in transport.calls
    with pytest.raises(lifecycle.LifecycleError, match="requires DEV stopped"):
        lifecycle.start(transport, snapshot())

    drifted = ScriptedTransport(slugs=(DEV, "core_ssh", "core_samba"))
    with pytest.raises(lifecycle.LifecycleError, match="catalog changed"):
        lifecycle.start(drifted, snapshot())
    assert f"POST /addons/{DEV}/start" not in drifted.calls

    other_image = ScriptedTransport()
    other_image.dev_version = "2.11.0.dev2"
    with pytest.raises(lifecycle.LifecycleError, match="version changed"):
        lifecycle.start(other_image, snapshot())
    assert f"POST /addons/{DEV}/start" not in other_image.calls


def test_start_fails_closed_when_dev_does_not_report_started() -> None:
    """A start request that is not confirmed by observation is a failure."""
    transport = ScriptedTransport()
    transport.overrides[f"/addons/{DEV}/start"] = None
    with pytest.raises(lifecycle.LifecycleError, match="did not start"):
        lifecycle.start(transport, snapshot())


def test_restore_stops_dev_first_even_if_stock_reappeared() -> None:
    """The stop never waits on catalog invariants; drift is reported after the stop."""
    transport = ScriptedTransport(dev_started=True, slugs=(DEV, STOCK))
    with pytest.raises(lifecycle.LifecycleError, match="stock Music Assistant is installed"):
        lifecycle.restore(transport, snapshot(), time.monotonic() + 5)
    assert transport.dev_started is False
    assert transport.calls.index(f"POST /addons/{DEV}/stop") < transport.calls.index("GET /addons")


def test_restore_is_idempotent_and_touches_nothing_else() -> None:
    """A stopped DEV is left alone; the only mutation ever issued is DEV stop."""
    transport = ScriptedTransport(dev_started=True)
    lifecycle.restore(transport, snapshot(), time.monotonic() + 5)
    lifecycle.restore(transport, snapshot(), time.monotonic() + 5)
    posts = [call for call in transport.calls if call.startswith("POST")]
    assert posts == [f"POST /addons/{DEV}/stop"]


def test_restore_never_verifies_an_unconfirmed_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stop response is not stop evidence; the observation must say `stopped`."""
    monkeypatch.setattr(lifecycle, "STOP_POLL_INTERVAL", 0.01)
    transport = ScriptedTransport(dev_started=True)
    transport.stop_works = False
    with pytest.raises(lifecycle.LifecycleError, match="did not stop"):
        lifecycle.restore(transport, snapshot(), time.monotonic() + 0.3)
    assert transport.calls.count(f"POST /addons/{DEV}/stop") == 1


@pytest.mark.parametrize("state", ["startup", "error", "unknown"])
def test_restore_sends_stop_from_any_non_stopped_state(
    monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    """`startup`/`error`/`unknown` within the window still get a stop, then an observation."""
    monkeypatch.setattr(lifecycle, "STOP_POLL_INTERVAL", 0.01)
    transport = ScriptedTransport(dev_started=True)
    observations = [state, state, "stopped"]

    def info(method: str, path: str) -> Any:
        if path == f"/addons/{DEV}/info" and observations:
            transport.calls.append(f"{method} {path}")
            return {
                "slug": DEV,
                "state": observations.pop(0),
                "version": "2.11.0.dev",
                "protected": True,
                "boot": "manual",
                "watchdog": False,
            }
        return ScriptedTransport.request(transport, method, path)

    monkeypatch.setattr(transport, "request", info)
    lifecycle.restore(transport, snapshot(), time.monotonic() + 5)
    assert transport.calls.count(f"POST /addons/{DEV}/stop") == 1
    assert transport.calls.index(f"POST /addons/{DEV}/stop") == 1


def test_restore_retries_transient_reads_until_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient Supervisor failure is retried inside the budget, not treated as stopped."""
    monkeypatch.setattr(lifecycle, "STOP_POLL_INTERVAL", 0.01)
    transport = ScriptedTransport(dev_started=True)
    failures = [2]

    def flaky(method: str, path: str) -> Any:
        if failures[0] and path == f"/addons/{DEV}/info":
            failures[0] -= 1
            raise lifecycle.TransientAvailabilityError("HTTP request failed")
        return ScriptedTransport.request(transport, method, path)

    monkeypatch.setattr(transport, "request", flaky)
    lifecycle.restore(transport, snapshot(), time.monotonic() + 5)
    assert transport.dev_started is False


def test_watchdog_records_an_unavailable_lock_as_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lock that never frees yields a failed receipt instead of silence."""
    monkeypatch.setattr(lifecycle, "LOCK_TIMEOUT", 0.3)
    state = snapshot()
    run_dir = lifecycle.save_snapshot(tmp_path, state)
    lifecycle._write_json_exclusive(
        run_dir / "watchdog.json",
        {"run_id": state.run_id, "nonce": state.nonce, "deadline": time.time()},
    )
    transport = ScriptedTransport(dev_started=True)
    with (
        lifecycle._mutation_lock(tmp_path, time.monotonic() + 5),
        pytest.raises(lifecycle.LifecycleError, match="deadline expired"),
    ):
        lifecycle.watchdog(tmp_path, state.run_id, transport)
    receipt = json.loads((run_dir / "receipt.json").read_text())
    assert receipt["verified"] is False
    assert receipt["status"].startswith("mutation lock unavailable")
    assert transport.calls == []
    # once the lock frees, the same run still stops DEV and promotes its receipt
    lifecycle.watchdog(tmp_path, state.run_id, transport)
    assert transport.dev_started is False
    assert lifecycle._has_verified_receipt(run_dir, state) is True


def test_stop_gets_a_full_budget_after_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Waiting on the lock must not eat the stop's own budget."""
    state = snapshot()
    run_dir = lifecycle.save_snapshot(tmp_path, state)
    lifecycle._write_json_exclusive(
        run_dir / "watchdog.json",
        {"run_id": state.run_id, "nonce": state.nonce, "deadline": time.time()},
    )
    transport = ScriptedTransport(dev_started=True)
    lock = lifecycle._mutation_lock(tmp_path, time.monotonic() + 5)
    lock.__enter__()
    release = threading.Timer(0.5, lambda: lock.__exit__(None, None, None))
    release.start()
    try:
        monkeypatch.setattr(lifecycle, "RESTORE_TIMEOUT", 0.2)
        lifecycle.watchdog(tmp_path, state.run_id, transport)
    finally:
        release.join()
    assert transport.dev_started is False
    assert lifecycle._has_verified_receipt(run_dir, state) is True


def test_repeat_failure_keeps_first_failed_receipt(tmp_path: Path) -> None:
    """The first failure is the evidence of record; a repeat failure never masks its cause."""
    state = snapshot()
    run_dir = lifecycle.save_snapshot(tmp_path, state)
    lifecycle._write_receipt(run_dir, state, False, "first")
    lifecycle._write_receipt(run_dir, state, False, "second")
    assert json.loads((run_dir / "receipt.json").read_text())["status"] == "first"


def test_restore_rejects_expired_deadline_before_any_request() -> None:
    """An exhausted budget issues no request at all."""
    transport = ScriptedTransport(dev_started=True)
    with pytest.raises(lifecycle.LifecycleError, match="deadline expired"):
        lifecycle.restore(transport, snapshot(), time.monotonic() - 1)
    assert transport.calls == []


def test_path_allowlist_has_no_stock_store_or_core_paths() -> None:
    """Structural office/stock exclusion: the transport cannot address anything else."""
    transport = lifecycle.SupervisorTransport("private-token", time.monotonic() + 5)
    for path in (
        f"/addons/{STOCK}/info",
        f"/addons/{STOCK}/start",
        f"/addons/{DEV}/install",
        f"/addons/{DEV}/restart",
        f"/addons/{DEV}/options",
        "/store/addons",
        f"/store/addons/{STOCK}/install",
        "/core/api/states/media_player.officepod",
        "/core/api/services/media_player/join",
        "/addons/",
        "/addons/x",
    ):
        with pytest.raises(lifecycle.LifecycleError, match="invalid fixed API path"):
            transport.request("GET", path)
    with pytest.raises(lifecycle.LifecycleError, match="invalid fixed API path"):
        transport.request("DELETE", "/addons")


# --- Persistence, arming and receipts -----------------------------------------------------


def test_snapshot_roundtrip_and_schema_rejections(tmp_path: Path) -> None:
    """Schema 2 snapshots round-trip; the historical schema 1 shape is never loaded."""
    state = snapshot()
    run_dir = lifecycle.save_snapshot(tmp_path, state)
    assert oct(run_dir.stat().st_mode & 0o777) == "0o700"
    assert oct((run_dir / "snapshot.json").stat().st_mode & 0o777) == "0o600"
    loaded_dir, loaded = lifecycle.load_snapshot(tmp_path, state.run_id)
    assert loaded_dir == run_dir
    assert loaded == state
    with pytest.raises(lifecycle.LifecycleError, match="already exists"):
        lifecycle.save_snapshot(tmp_path, state)

    legacy = tmp_path / "223e4567-e89b-12d3-a456-426614174000"
    legacy.mkdir(mode=0o700)
    (legacy / "snapshot.json").write_text(
        json.dumps({"schema": 1, "snapshot": {"run_id": legacy.name}})
    )
    (legacy / "snapshot.json").chmod(0o600)
    with pytest.raises(lifecycle.LifecycleError, match="schema is unknown"):
        lifecycle.load_snapshot(tmp_path, legacy.name)


@pytest.mark.parametrize(
    "damage",
    [
        {"installed_slugs": ["core_ssh", DEV, STOCK]},
        {"installed_slugs": ["core_ssh"]},
        {"installed_slugs": [DEV, "core_ssh"]},
        {"installed_slugs": []},
        {"nonce": "A" * 64},
        {"dev_version": ""},
        {"created_at": "now"},
        {"extra": True},
    ],
)
def test_load_rejects_malformed_or_stock_bearing_snapshots(tmp_path: Path, damage: Any) -> None:
    """A snapshot that could describe a stock-present state is never loadable."""
    state = snapshot()
    run_dir = lifecycle.save_snapshot(tmp_path, state)
    raw = json.loads((run_dir / "snapshot.json").read_text())
    raw["snapshot"].update(damage)
    (run_dir / "snapshot.json").write_text(json.dumps(raw))
    with pytest.raises(lifecycle.LifecycleError, match="malformed"):
        lifecycle.load_snapshot(tmp_path, state.run_id)


def test_load_rejects_invalid_run_identity(tmp_path: Path) -> None:
    """Run ids are canonical UUIDs only; nothing else resolves to a directory."""
    for run_id in ("../x", "", "123E4567-E89B-12D3-A456-426614174000", "snapshot"):
        with pytest.raises(lifecycle.LifecycleError, match="invalid lifecycle run identity"):
            lifecycle.load_snapshot(tmp_path, run_id)


def test_arm_rejects_stale_snapshot_bad_timeout_and_completed_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Arming requires a fresh run, a bounded timeout and no completion evidence."""
    monkeypatch.setenv("SUPERVISOR_TOKEN", "private-token")
    stale = snapshot(created_at=time.time() - lifecycle.MAX_RUN_AGE - 1)
    lifecycle.save_snapshot(tmp_path, stale)
    with pytest.raises(lifecycle.LifecycleError, match="too old"):
        lifecycle.arm_watchdog(tmp_path, stale.run_id, 10)

    state = snapshot(run_id="223e4567-e89b-12d3-a456-426614174000")
    run_dir = lifecycle.save_snapshot(tmp_path, state)
    for timeout in (0, -1, float("inf"), float("nan"), lifecycle.MAX_WATCHDOG_TIMEOUT + 1):
        with pytest.raises(lifecycle.LifecycleError, match="timeout"):
            lifecycle.arm_watchdog(tmp_path, state.run_id, timeout)
    (run_dir / "receipt.json").symlink_to("/nonexistent")
    with pytest.raises(lifecycle.LifecycleError, match="already has a receipt"):
        lifecycle.arm_watchdog(tmp_path, state.run_id, 10)
    assert not (run_dir / "watchdog.json").exists()


def test_arm_requires_token_outside_self_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live watchdog cannot be armed without the Supervisor token."""
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    state = snapshot()
    lifecycle.save_snapshot(tmp_path, state)
    with pytest.raises(lifecycle.LifecycleError, match="SUPERVISOR_TOKEN"):
        lifecycle.arm_watchdog(tmp_path, state.run_id, 10)


@contextmanager
def _held_lease(root: Path, owner: dict[str, Any] | None) -> Generator[None]:
    """Hold the watchdog lease in-process the way a live detached child does."""
    descriptor = lifecycle._acquire_watchdog_lease(root)
    try:
        os.ftruncate(descriptor, 0)
        if owner is not None:
            os.write(descriptor, json.dumps(owner).encode())
        yield
    finally:
        os.close(descriptor)


def test_require_armed_gates_start(tmp_path: Path) -> None:
    """DEV may start only inside a live, unexpired, receipt-free, non-simulated run."""
    state = snapshot()
    run_dir = lifecycle.save_snapshot(tmp_path, state)
    owner = {"run_id": state.run_id, "nonce": state.nonce}
    with pytest.raises(lifecycle.LifecycleError, match="lifecycle file is unsafe"):
        lifecycle._require_armed(tmp_path, run_dir, state)
    lifecycle._write_json_exclusive(
        run_dir / "watchdog.json",
        {"run_id": state.run_id, "nonce": state.nonce, "deadline": time.time() - 1},
    )
    with pytest.raises(lifecycle.LifecycleError, match="deadline already passed"):
        lifecycle._require_armed(tmp_path, run_dir, state)
    (run_dir / "watchdog.json").unlink()
    lifecycle._write_json_exclusive(
        run_dir / "watchdog.json",
        {"run_id": state.run_id, "nonce": state.nonce, "deadline": time.time() + 60},
    )
    # watchdog.json alone (failed spawn, dead child) is not a live watchdog
    with pytest.raises(lifecycle.LifecycleError, match="no live watchdog"):
        lifecycle._require_armed(tmp_path, run_dir, state)
    with (
        _held_lease(tmp_path, {"run_id": state.run_id, "nonce": "b" * 64}),
        pytest.raises(lifecycle.LifecycleError, match="belongs to another run"),
    ):
        lifecycle._require_armed(tmp_path, run_dir, state)
    with (
        _held_lease(tmp_path, None),
        pytest.raises(lifecycle.LifecycleError, match="lease is malformed"),
    ):
        lifecycle._require_armed(tmp_path, run_dir, state)
    with _held_lease(tmp_path, owner):
        lifecycle._require_armed(tmp_path, run_dir, state)
        lifecycle._write_json_exclusive(run_dir / "receipt.json", {"any": "thing"})
        with pytest.raises(lifecycle.LifecycleError, match="already has a receipt"):
            lifecycle._require_armed(tmp_path, run_dir, state)
        (run_dir / "receipt.json").unlink()
        lifecycle._write_json_exclusive(
            run_dir / "simulation.json", {"run_id": state.run_id, "nonce": state.nonce}
        )
        with pytest.raises(lifecycle.LifecycleError, match="self-test run can never start"):
            lifecycle._require_armed(tmp_path, run_dir, state)


def test_armed_child_lease_names_its_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A real detached child keeps a lease that `start` can attribute to its run."""
    monkeypatch.setenv("SUPERVISOR_TOKEN", "private-token")
    state = snapshot()
    run_dir = lifecycle.save_snapshot(tmp_path, state)
    pid = lifecycle.arm_watchdog(tmp_path, state.run_id, 5)
    try:
        time.sleep(0.2)
        lifecycle._require_armed(tmp_path, run_dir, state)
    finally:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
    with pytest.raises(lifecycle.LifecycleError, match="no live watchdog"):
        lifecycle._require_armed(tmp_path, run_dir, state)


def test_main_start_is_gated_before_any_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI `start` never reaches the transport without a live armed run."""
    monkeypatch.setenv("SUPERVISOR_TOKEN", "private-token")
    transport = ScriptedTransport()
    monkeypatch.setattr(lifecycle, "SupervisorTransport", lambda *_args: transport)
    state = snapshot()
    run_dir = lifecycle.save_snapshot(tmp_path, state)
    argv = ["start", "--root", str(tmp_path), "--run", state.run_id]
    assert lifecycle.main(argv) == 1  # unarmed
    lifecycle._write_json_exclusive(
        run_dir / "watchdog.json",
        {"run_id": state.run_id, "nonce": state.nonce, "deadline": time.time() + 60},
    )
    assert lifecycle.main(argv) == 1  # no live lease
    with _held_lease(tmp_path, {"run_id": state.run_id, "nonce": state.nonce}):
        lifecycle._write_json_exclusive(run_dir / "receipt.json", {"any": "thing"})
        assert lifecycle.main(argv) == 1  # receipted
        (run_dir / "receipt.json").unlink()
        assert transport.calls == []
        assert lifecycle.main(argv) == 0
    assert f"POST /addons/{DEV}/start" in transport.calls
    assert transport.dev_started is True


def test_watchdog_deadline_is_bounded(tmp_path: Path) -> None:
    """A hand-edited far-future deadline cannot stretch the sleep or the start window."""
    state = snapshot()
    run_dir = lifecycle.save_snapshot(tmp_path, state)
    lifecycle._write_json_exclusive(
        run_dir / "watchdog.json",
        {
            "run_id": state.run_id,
            "nonce": state.nonce,
            "deadline": time.time() + lifecycle.MAX_WATCHDOG_TIMEOUT + 5,
        },
    )
    with pytest.raises(lifecycle.LifecycleError, match="does not own"):
        lifecycle._load_watchdog(run_dir, state)


def test_watchdog_rejects_wrong_owner_and_writes_failure_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A foreign nonce never runs; a failed stop leaves a failed receipt that stays failed."""
    monkeypatch.setattr(lifecycle, "RESTORE_TIMEOUT", 0.3)
    monkeypatch.setattr(lifecycle, "STOP_POLL_INTERVAL", 0.01)
    state = snapshot()
    run_dir = lifecycle.save_snapshot(tmp_path, state)
    lifecycle._write_json_exclusive(
        run_dir / "watchdog.json",
        {"run_id": state.run_id, "nonce": "b" * 64, "deadline": time.time()},
    )
    with pytest.raises(lifecycle.LifecycleError, match="does not own"):
        lifecycle.watchdog(tmp_path, state.run_id, ScriptedTransport())
    (run_dir / "watchdog.json").unlink()
    lifecycle._write_json_exclusive(
        run_dir / "watchdog.json",
        {"run_id": state.run_id, "nonce": state.nonce, "deadline": time.time()},
    )
    stuck = ScriptedTransport(dev_started=True)
    stuck.stop_works = False
    with pytest.raises(lifecycle.LifecycleError, match="did not stop"):
        lifecycle.watchdog(tmp_path, state.run_id, stuck)
    receipt = json.loads((run_dir / "receipt.json").read_text())
    assert receipt == {
        "run_id": state.run_id,
        "nonce": state.nonce,
        "verified": False,
        "status": "DEV app did not stop",
    }
    assert lifecycle._has_verified_receipt(run_dir, state) is False

    # a later successful stop promotes the receipt and archives the failed one
    lifecycle.watchdog(tmp_path, state.run_id, ScriptedTransport(dev_started=True))
    assert json.loads((run_dir / "receipt.json").read_text()) == {
        "run_id": state.run_id,
        "nonce": state.nonce,
        "verified": True,
        "status": lifecycle.VERIFIED_STATUS,
    }
    assert json.loads((run_dir / "failed_receipt.json").read_text()) == receipt
    assert lifecycle._has_verified_receipt(run_dir, state) is True

    # a settled run issues no further requests at all
    settled = ScriptedTransport(dev_started=True)
    lifecycle.watchdog(tmp_path, state.run_id, settled)
    assert settled.calls == []
    assert settled.dev_started is True


def test_foreign_or_v1_receipt_never_settles_a_run(tmp_path: Path) -> None:
    """Only this controller's exact verified receipt counts; v1 receipts are not reused."""
    state = snapshot()
    run_dir = lifecycle.save_snapshot(tmp_path, state)
    for receipt in (
        {
            "run_id": state.run_id,
            "nonce": state.nonce,
            "verified": True,
            "status": "HA app/state restoration verified; native peers excluded",
        },
        {
            "run_id": state.run_id,
            "nonce": "b" * 64,
            "verified": True,
            "status": lifecycle.VERIFIED_STATUS,
        },
        {
            "run_id": state.run_id,
            "nonce": state.nonce,
            "verified": "true",
            "status": lifecycle.VERIFIED_STATUS,
        },
    ):
        (run_dir / "receipt.json").unlink(missing_ok=True)
        lifecycle._write_json_exclusive(run_dir / "receipt.json", receipt)
        assert lifecycle._has_verified_receipt(run_dir, state) is False
        with pytest.raises(lifecycle.LifecycleError, match="conflicts"):
            lifecycle._write_receipt(run_dir, state, True, lifecycle.VERIFIED_STATUS)


def test_self_test_detaches_and_verifies_receipt(tmp_path: Path) -> None:
    """The CLI's fake-only watchdog path proves child identity and receipt handling."""
    lifecycle.self_test(tmp_path)
    receipts = list(tmp_path.glob("*/receipt.json"))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_text())["verified"] is True
    assert (receipts[0].parent / "simulation.json").exists()


def test_self_test_child_requires_matching_simulation_marker(tmp_path: Path) -> None:
    """The fake transport cannot be selected for an ordinary saved run."""
    state = snapshot()
    lifecycle.save_snapshot(tmp_path, state)
    assert lifecycle.main(["self-test-child", "--root", str(tmp_path), "--run", state.run_id]) == 1


def test_overlapping_watchdogs_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One application-wide lease: a second arm fails while the first child holds it."""
    monkeypatch.setenv("SUPERVISOR_TOKEN", "private-token")
    first = snapshot()
    second = snapshot(run_id="223e4567-e89b-12d3-a456-426614174000")
    lifecycle.save_snapshot(tmp_path, first)
    lifecycle.save_snapshot(tmp_path, second)
    pid = lifecycle.arm_watchdog(tmp_path, first.run_id, 5)
    try:
        time.sleep(0.2)
        with pytest.raises(lifecycle.LifecycleError, match="already armed"):
            lifecycle.arm_watchdog(tmp_path, second.run_id, 5)
    finally:
        os.kill(pid, 9)
        os.waitpid(pid, 0)


# --- Native curl wire contract -------------------------------------------------------------


def test_supervisor_transport_real_wire_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """Native curl unwraps Supervisor envelopes and sends auth only in stdin config."""
    catalog = json.dumps(
        {
            "result": "ok",
            "data": {
                "addons": [
                    {"slug": "core_ssh", "state": "started", "version": "9.0.0"},
                    {"slug": DEV, "state": "stopped", "version": "2.11.0.dev"},
                ]
            },
        }
    ).encode()
    dev = json.dumps(
        {
            "result": "ok",
            "data": {
                "slug": DEV,
                "state": "stopped",
                "version": "2.11.0.dev",
                "protected": True,
                "boot": "manual",
                "watchdog": False,
            },
        }
    ).encode()
    with _fixed_http_server({"/addons": (200, catalog), f"/addons/{DEV}/info": (200, dev)}) as (
        base_url,
        calls,
    ):
        monkeypatch.setattr(lifecycle, "SUPERVISOR_BASE_URL", base_url)
        facts = lifecycle.read_facts(
            lifecycle.SupervisorTransport("private-token", time.monotonic() + 5)
        )
    assert facts == lifecycle.Facts(("core_ssh", DEV), "stopped", "2.11.0.dev")
    assert [call[1] for call in calls] == ["/addons", f"/addons/{DEV}/info"]
    assert all(call[2]["Authorization"] == "Bearer private-token" for call in calls)


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        json.dumps({"result": "error", "message": "x"}).encode(),
        json.dumps({"data": {}}).encode(),
        json.dumps([]).encode(),
    ],
)
def test_supervisor_transport_rejects_malformed_envelopes(
    monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    """Anything but a `result: ok` envelope with `data` is rejected."""
    with _fixed_http_server({"/addons": (200, body)}) as (base_url, _calls):
        monkeypatch.setattr(lifecycle, "SUPERVISOR_BASE_URL", base_url)
        transport = lifecycle.SupervisorTransport("private-token", time.monotonic() + 5)
        with pytest.raises(lifecycle.LifecycleError, match="Supervisor"):
            transport.request("GET", "/addons")


def test_supervisor_transport_rejects_unsuccessful_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-2xx status is a failure, never a fallback shape."""
    with _fixed_http_server({"/addons": (404, b'{"result":"error"}')}) as (base_url, _calls):
        monkeypatch.setattr(lifecycle, "SUPERVISOR_BASE_URL", base_url)
        transport = lifecycle.SupervisorTransport("private-token", time.monotonic() + 5)
        with pytest.raises(lifecycle.LifecycleError, match="unsuccessful HTTP status"):
            transport.request("GET", "/addons")


def test_supervisor_transport_requires_clean_token() -> None:
    """Empty or control-character tokens never reach curl."""
    for token in ("", "a\nb", "a\r", "a\x00"):
        with pytest.raises(lifecycle.LifecycleError, match="SUPERVISOR_TOKEN"):
            lifecycle.SupervisorTransport(token, time.monotonic() + 5)


def test_main_reports_failures_as_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI never raises through; failures are exit code 1."""
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    assert lifecycle.main(["preflight", "--root", str(tmp_path)]) == 1
    assert lifecycle.main(["arm", "--root", str(tmp_path)]) == 1
    assert lifecycle.main(["start", "--root", str(tmp_path), "--run", "nope"]) == 1
    assert lifecycle.main(["self-test", "--root", str(tmp_path)]) == 0
