"""Offline tests for the bounded private TV RAOP transport controller."""

from __future__ import annotations

import json
import threading
import time
from contextlib import suppress
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from scripts import private_tv_transport as transport
from scripts.private_trial_scope import TrialScope
from scripts.private_tv_transport import (
    HTTP_TIMEOUT_SECONDS,
    MusicAssistantApi,
    StimulusServer,
    StopWatchdog,
    TransportError,
    generate_stimulus,
    run_once,
)

PLAY_SLACK_SECONDS = 2
WATCHDOG_STOP_OFFSET_SECONDS = 2


def tv_scope() -> TrialScope:
    """Return a synthetic, explicitly scoped TV inventory."""
    return TrialScope.parse(
        {
            "target": "021122334455@test-tv",
            "target_ip": "192.0.2.10",
            "target_link_mac": "02:11:22:33:44:55",
            "excluded": [{"ip": "198.51.100.20", "mac": "02:aa:bb:cc:dd:ee"}],
            "host_ip": "203.0.113.30",
            "ssh_host": "trial-host",
            "mode": "tv-raop",
        }
    )


class FakeStopGuard:
    """Record watchdog arming without spawning a process in unit tests."""

    def __init__(self) -> None:
        """Create an empty watchdog call log."""
        self.calls: list[tuple[str, float]] = []

    def arm(self, player: str, deadline: float) -> None:
        """Record the pre-dispatch stop deadline."""
        self.calls.append((player, deadline))


class FakeApi:
    """Record supported MA API calls without a real server or credential."""

    def __init__(self, *, fetch_urls: list[str] | None = None, play_delay: float = 0.0) -> None:
        """Keep the exact local URLs MA is simulated to fetch, in order, on play."""
        self.fetch_urls = fetch_urls or []
        self.play_delay = play_delay
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.stops: list[tuple[float, float]] = []

    def command(self, command: str, args: dict[str, Any], timeout: float = 3.0) -> Any:
        """Return only the exact synthetic player inventory."""
        self.calls.append((command, args))
        if command == "players/cmd/stop":
            self.stops.append((time.monotonic(), timeout))
        if command == "players/all":
            return [
                {
                    "player_id": "ap021122334455",
                    "device_info": {"manufacturer": "Apple", "model": "Apple TV 4K"},
                }
            ]
        if command == "player_queues/play_media":
            time.sleep(self.play_delay)
            for fetch_url in self.fetch_urls:
                parsed = urlsplit(fetch_url)
                connection = HTTPConnection(parsed.netloc, timeout=1)
                connection.request("GET", parsed.path)
                connection.getresponse().read()
                connection.close()
        return {}


def test_stimulus_is_deterministic_and_exactly_finite() -> None:
    """Generate one stable WAV payload with the requested tone and silence duration."""
    first = generate_stimulus()
    second = generate_stimulus()
    assert first == second
    assert len(first.sha256) == 64
    assert first.payload[:4] == b"RIFF"


@pytest.fixture
def short_budget(monkeypatch: pytest.MonkeyPatch) -> float:
    """Shrink the observation window, keeping the reserved stop interval real."""
    observation = 0.5
    monkeypatch.setattr(transport, "STOP_BUDGET_SECONDS", observation + HTTP_TIMEOUT_SECONDS)
    return observation


def test_default_budget_observes_the_whole_wave_and_reserves_stop() -> None:
    """Pin the documented window: 4s wave plus slack, then one full HTTP stop interval."""
    wave_seconds = transport.TONE_SECONDS + transport.SILENCE_SECONDS
    assert wave_seconds + PLAY_SLACK_SECONDS <= transport.OBSERVATION_SECONDS
    assert transport.STOP_BUDGET_SECONDS == transport.OBSERVATION_SECONDS + HTTP_TIMEOUT_SECONDS


@pytest.mark.usefixtures("short_budget")
def test_controller_records_observed_fetch_and_stops_in_finally() -> None:
    """Constrain one accepted play request to the generated endpoint and always stop."""
    stimulus = generate_stimulus()
    server = StimulusServer("127.0.0.1", stimulus)
    server.start()
    try:
        url = f"http://127.0.0.1:{server.port}/stimulus.wav"
        api = FakeApi(fetch_urls=[url])
        guard = FakeStopGuard()
        receipt = run_once(tv_scope(), api, url, server, stimulus, stop_guard=guard)
    finally:
        server.close()
    assert receipt.outcome == "INCOMPLETE"
    assert receipt.completed_fetches == 1
    assert receipt.ma_play_request_accepted is True
    assert receipt.ma_stop_requested is True
    assert receipt.child_connected is None
    assert receipt.child_pcm is None
    assert (
        receipt.limitations[-1] == "No RTP or packet evidence is produced or accepted by this tool."
    )
    assert api.calls[1] == (
        "player_queues/play_media",
        {"queue_id": "ap021122334455", "media": url, "option": "replace"},
    )
    assert api.calls[-1] == ("players/cmd/stop", {"player_id": "ap021122334455"})
    assert guard.calls[0][0] == "ap021122334455"


def test_early_and_repeated_fetches_do_not_shorten_observation(short_budget: float) -> None:
    """Keep observing after a metadata-style first fetch until the reserved stop point."""
    stimulus = generate_stimulus()
    server = StimulusServer("127.0.0.1", stimulus)
    server.start()
    try:
        url = f"http://127.0.0.1:{server.port}/stimulus.wav"
        api = FakeApi(fetch_urls=[url] * 3)
        started = time.monotonic()
        receipt = run_once(tv_scope(), api, url, server, stimulus, stop_guard=FakeStopGuard())
    finally:
        server.close()
    assert receipt.completed_fetches == 3
    assert receipt.outcome == "INCOMPLETE"
    assert receipt.error is None
    assert len(api.stops) == 1
    stopped_at, timeout = api.stops[0]
    assert stopped_at - started >= short_budget
    assert timeout <= HTTP_TIMEOUT_SECONDS


def test_slow_play_response_still_leaves_the_stop_interval(short_budget: float) -> None:
    """A play call that eats the observation window must not skip or lengthen the stop."""
    stimulus = generate_stimulus()
    server = StimulusServer("127.0.0.1", stimulus)
    server.start()
    try:
        api = FakeApi(play_delay=short_budget + 0.2)
        started = time.monotonic()
        receipt = run_once(
            tv_scope(),
            api,
            f"http://127.0.0.1:{server.port}/stimulus.wav",
            server,
            stimulus,
            stop_guard=FakeStopGuard(),
        )
    finally:
        server.close()
    assert receipt.ma_stop_requested is True
    stopped_at, timeout = api.stops[0]
    assert timeout < HTTP_TIMEOUT_SECONDS
    assert stopped_at - started <= short_budget + HTTP_TIMEOUT_SECONDS


def test_expired_stop_budget_is_an_error_not_a_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the budget is gone the parent records failure instead of a late stop."""
    monkeypatch.setattr(transport, "HTTP_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(transport, "STOP_BUDGET_SECONDS", 0.3)
    stimulus = generate_stimulus()
    api = FakeApi(play_delay=0.4)
    receipt = run_once(
        tv_scope(),
        api,
        "http://127.0.0.1:9/stimulus.wav",
        StimulusServer("127.0.0.1", stimulus),
        stimulus,
        stop_guard=FakeStopGuard(),
    )
    assert receipt.ma_stop_requested is False
    assert receipt.error == "stop budget expired"
    assert api.stops == []


@pytest.mark.usefixtures("short_budget")
def test_cancel_during_observation_still_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    """An interrupt mid-window propagates but the finally block still requests stop."""
    stimulus = generate_stimulus()
    api = FakeApi()

    def interrupt(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(time, "sleep", interrupt)
    with pytest.raises(KeyboardInterrupt):
        run_once(
            tv_scope(),
            api,
            "http://127.0.0.1:9/stimulus.wav",
            StimulusServer("127.0.0.1", stimulus),
            stimulus,
            stop_guard=FakeStopGuard(),
        )
    assert api.calls[-1] == ("players/cmd/stop", {"player_id": "ap021122334455"})


def test_arming_failure_is_error_and_never_plays() -> None:
    """A watchdog that cannot arm fails closed before any play request."""

    class BrokenGuard:
        def arm(self, player: str, deadline: float) -> None:
            raise TransportError("could not arm independent stop watchdog")

    stimulus = generate_stimulus()
    api = FakeApi()
    receipt = run_once(
        tv_scope(),
        api,
        "http://127.0.0.1:9/stimulus.wav",
        StimulusServer("127.0.0.1", stimulus),
        stimulus,
        stop_guard=BrokenGuard(),
    )
    assert receipt.error == "could not arm independent stop watchdog"
    assert all(command != "player_queues/play_media" for command, _ in api.calls)
    assert receipt.ma_play_request_accepted is False


@pytest.mark.usefixtures("short_budget")
def test_stop_failure_is_recorded_after_full_observation() -> None:
    """A failed final stop keeps the error and never reports a stop request."""

    class FailingStop(FakeApi):
        def command(self, command: str, args: dict[str, Any], timeout: float = 3.0) -> Any:
            if command == "players/cmd/stop":
                raise TransportError("stop failed")
            return super().command(command, args, timeout)

    stimulus = generate_stimulus()
    receipt = run_once(
        tv_scope(),
        FailingStop(),
        "http://127.0.0.1:9/stimulus.wav",
        StimulusServer("127.0.0.1", stimulus),
        stimulus,
        stop_guard=FakeStopGuard(),
    )
    assert receipt.ma_stop_requested is False
    assert receipt.error == "stop failed"


@pytest.mark.usefixtures("short_budget")
def test_missing_fetch_is_incomplete_but_still_stops() -> None:
    """Never promote an accepted MA command into playback evidence without a fetch."""
    stimulus = generate_stimulus()
    server = StimulusServer("127.0.0.1", stimulus)
    server.start()
    try:
        receipt = run_once(
            tv_scope(),
            FakeApi(),
            f"http://127.0.0.1:{server.port}/stimulus.wav",
            server,
            stimulus,
            stop_guard=FakeStopGuard(),
        )
    finally:
        server.close()
    assert receipt.outcome == "INCOMPLETE"
    assert receipt.completed_fetches == 0
    assert receipt.ma_stop_requested is True


def test_invalid_scope_refuses_before_playback() -> None:
    """Do not reuse the transport controller for the HomePod lane."""
    stimulus = generate_stimulus()
    scope = TrialScope.parse(
        {
            "target": "021122334455@test-speaker",
            "target_ip": "192.0.2.10",
            "target_link_mac": "02:11:22:33:44:55",
            "excluded": [{"ip": "198.51.100.20"}],
            "host_ip": "203.0.113.30",
            "ssh_host": "trial-host",
            "mode": "homepod-ptp",
        }
    )
    server = StimulusServer("127.0.0.1", stimulus)
    with pytest.raises(TransportError, match="tv-raop"):
        run_once(
            scope,
            FakeApi(),
            "http://127.0.0.1/stimulus.wav",
            server,
            stimulus,
            stop_guard=FakeStopGuard(),
        )


def test_slow_drip_api_response_cannot_extend_total_timeout() -> None:
    """Apply the timeout to headers and body transfer, not each socket read."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            """Trickle a syntactically irrelevant response beyond the hard deadline."""
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            for _ in range(100):
                self.wfile.write(b"x")
                self.wfile.flush()
                time.sleep(0.1)

        def log_message(self, _format: str, *_args: object) -> None:
            """Keep the regression test quiet."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        started = time.monotonic()
        with pytest.raises(TransportError, match="request failed"):
            MusicAssistantApi(f"http://127.0.0.1:{server.server_port}", "token").command(
                "players/all", {}
            )
        assert time.monotonic() - started < HTTP_TIMEOUT_SECONDS + 1
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_independent_watchdog_stops_after_slow_play_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the stop delivery independent from a slow accepted playback response."""
    # The parent's play timeout is HTTP_TIMEOUT_SECONDS, so a watchdog stop earlier than
    # that is provably not parent cleanup.
    monkeypatch.setattr(
        transport, "STOP_BUDGET_SECONDS", WATCHDOG_STOP_OFFSET_SECONDS + HTTP_TIMEOUT_SECONDS
    )
    stopped = threading.Event()
    stopped_at: list[float] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            """Model a valid player read, a slow play response, and an observed stop."""
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            command = request["command"]
            if command == "players/all":
                payload = json.dumps(
                    [
                        {
                            "player_id": "ap021122334455",
                            "device_info": {
                                "manufacturer": "Apple",
                                "model": "Apple TV 4K",
                            },
                        }
                    ]
                ).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if command == "player_queues/play_media":
                self.send_response(200)
                self.send_header("Content-Length", "100")
                self.end_headers()
                for _ in range(100):
                    with suppress(BrokenPipeError):
                        self.wfile.write(b"x")
                        self.wfile.flush()
                    time.sleep(0.1)
                return
            assert command == "players/cmd/stop"
            stopped_at.append(time.monotonic())
            stopped.set()
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, _format: str, *_args: object) -> None:
            """Keep the regression test quiet."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    stimulus = generate_stimulus()
    source = StimulusServer("127.0.0.1", stimulus)
    source.start()
    try:
        url = f"http://127.0.0.1:{source.port}/stimulus.wav"
        api_url = f"http://127.0.0.1:{server.server_port}"
        started = time.monotonic()
        receipt = run_once(
            tv_scope(),
            MusicAssistantApi(api_url, "test-token"),
            url,
            source,
            stimulus,
            stop_guard=StopWatchdog(api_url, "test-token", "TV_TRANSPORT_TEST_TOKEN"),
        )
        assert receipt.ma_play_request_accepted is False
        assert stopped.wait(2)
        assert WATCHDOG_STOP_OFFSET_SECONDS - 0.5 <= stopped_at[0] - started < HTTP_TIMEOUT_SECONDS
    finally:
        source.close()
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.usefixtures("short_budget")
def test_main_returns_incomplete_for_clean_source_and_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Do not promote a clean infrastructure observation into transport acceptance."""
    inventory = tmp_path / "scope.json"
    inventory.write_text(
        json.dumps(
            {
                "target": "021122334455@test-tv",
                "target_ip": "192.0.2.10",
                "target_link_mac": "02:11:22:33:44:55",
                "excluded": [{"ip": "198.51.100.20", "mac": "02:aa:bb:cc:dd:ee"}],
                "host_ip": "203.0.113.30",
                "ssh_host": "trial-host",
                "mode": "tv-raop",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(transport, "validate_lifecycle_run", lambda *_args: None)
    monkeypatch.setattr(transport, "MusicAssistantApi", lambda *_args: FakeApi())
    monkeypatch.setattr(transport, "StopWatchdog", lambda *_args: FakeStopGuard())

    result = transport.main(
        [
            "--inventory",
            str(inventory),
            "--ma-url",
            "http://127.0.0.1:8095",
            "--bind",
            "127.0.0.1",
            "--advertised-host",
            "127.0.0.1",
            "--lifecycle-root",
            str(tmp_path),
            "--lifecycle-run",
            "00000000-0000-0000-0000-000000000000",
        ]
    )

    assert result == 2
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["outcome"] == "INCOMPLETE"
    assert receipt["schema_version"] == 2
    assert receipt["completed_fetches"] == 0


@pytest.mark.usefixtures("short_budget")
def test_main_returns_failure_when_cleanup_records_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Do not classify a failed stop request as clean infrastructure observation."""
    inventory = tmp_path / "scope.json"
    inventory.write_text(
        json.dumps(
            {
                "target": "021122334455@test-tv",
                "target_ip": "192.0.2.10",
                "target_link_mac": "02:11:22:33:44:55",
                "excluded": [{"ip": "198.51.100.20", "mac": "02:aa:bb:cc:dd:ee"}],
                "host_ip": "203.0.113.30",
                "ssh_host": "trial-host",
                "mode": "tv-raop",
            }
        ),
        encoding="utf-8",
    )

    class ApiWithFailedStop(FakeApi):
        """Fail only the mandatory cleanup request."""

        def command(self, command: str, args: dict[str, Any], timeout: float = 3.0) -> Any:
            """Raise the transport error after the one playback command."""
            if command == "players/cmd/stop":
                raise TransportError("stop failed")
            return super().command(command, args, timeout)

    monkeypatch.setattr(transport, "validate_lifecycle_run", lambda *_args: None)
    monkeypatch.setattr(transport, "MusicAssistantApi", lambda *_args: ApiWithFailedStop())
    monkeypatch.setattr(transport, "StopWatchdog", lambda *_args: FakeStopGuard())

    result = transport.main(
        [
            "--inventory",
            str(inventory),
            "--ma-url",
            "http://127.0.0.1:8095",
            "--bind",
            "127.0.0.1",
            "--advertised-host",
            "127.0.0.1",
            "--lifecycle-root",
            str(tmp_path),
            "--lifecycle-run",
            "00000000-0000-0000-0000-000000000000",
        ]
    )

    assert result == 1
    assert json.loads(capsys.readouterr().out)["error"] == "stop failed"


def test_main_returns_failure_for_preflight_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Distinguish a failed setup from clean but insufficient transport facts."""
    inventory = tmp_path / "scope.json"
    inventory.write_text(
        json.dumps(
            {
                "target": "021122334455@test-tv",
                "target_ip": "192.0.2.10",
                "target_link_mac": "02:11:22:33:44:55",
                "excluded": [{"ip": "198.51.100.20", "mac": "02:aa:bb:cc:dd:ee"}],
                "host_ip": "203.0.113.30",
                "ssh_host": "trial-host",
                "mode": "tv-raop",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        transport,
        "validate_lifecycle_run",
        lambda *_args: (_ for _ in ()).throw(TransportError("watchdog missing")),
    )
    monkeypatch.setattr(
        transport,
        "StimulusServer",
        lambda *_args: pytest.fail("preflight must reject before constructing the source server"),
    )

    result = transport.main(
        [
            "--inventory",
            str(inventory),
            "--ma-url",
            "http://127.0.0.1:8095",
            "--bind",
            "127.0.0.1",
            "--advertised-host",
            "127.0.0.1",
            "--lifecycle-root",
            str(tmp_path),
            "--lifecycle-run",
            "00000000-0000-0000-0000-000000000000",
        ]
    )

    assert result == 1
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["outcome"] == "FAILED"
    assert receipt["error"] == "watchdog missing"


def test_only_the_stimulus_path_is_served() -> None:
    """Avoid accidentally exposing a directory or general-purpose local web server."""
    stimulus = generate_stimulus()
    server = StimulusServer("127.0.0.1", stimulus)
    server.start()
    try:
        connection = HTTPConnection(f"127.0.0.1:{server.port}", timeout=1)
        connection.request("GET", "/not-stimulus")
        assert connection.getresponse().status == 404
        connection.close()
    finally:
        server.close()
