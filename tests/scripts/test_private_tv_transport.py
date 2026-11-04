"""Offline tests for the bounded private TV RAOP transport controller."""

from __future__ import annotations

from http.client import HTTPConnection
from typing import Any
from urllib.parse import urlsplit

import pytest

from scripts.private_trial_scope import TrialScope
from scripts.private_tv_transport import (
    StimulusServer,
    TransportError,
    generate_stimulus,
    run_once,
)


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


class FakeApi:
    """Record supported MA API calls without a real server or credential."""

    def __init__(self, *, fetch: str | None = None) -> None:
        """Keep an optional exact local URL which simulates MA's single fetch."""
        self.fetch = fetch
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def command(self, command: str, args: dict[str, Any], timeout: float = 3.0) -> Any:
        """Return only the exact synthetic player inventory."""
        self.calls.append((command, args))
        if command == "players/all":
            return [
                {
                    "player_id": "ap021122334455",
                    "device_info": {"manufacturer": "Apple", "model": "Apple TV 4K"},
                }
            ]
        if command == "player_queues/play_media" and self.fetch:
            parsed = urlsplit(self.fetch)
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


def test_controller_records_observed_fetch_and_stops_in_finally() -> None:
    """Constrain one accepted play request to the generated endpoint and always stop."""
    stimulus = generate_stimulus()
    server = StimulusServer("127.0.0.1", stimulus)
    server.start()
    try:
        url = f"http://127.0.0.1:{server.port}/stimulus.wav"
        api = FakeApi(fetch=url)
        receipt = run_once(tv_scope(), api, url, server, stimulus)
    finally:
        server.close()
    assert receipt.outcome == "OBSERVED"
    assert receipt.fetched is True
    assert receipt.ma_play_request_accepted is True
    assert receipt.ma_stop_requested is True
    assert receipt.child_connected is None
    assert receipt.child_pcm is None
    assert receipt.packet_receipt is None
    assert api.calls[-1] == ("players/cmd/stop", {"player_id": "ap021122334455"})


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
        )
    finally:
        server.close()
    assert receipt.outcome == "INCOMPLETE"
    assert receipt.fetched is False
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
        run_once(scope, FakeApi(), "http://127.0.0.1/stimulus.wav", server, stimulus)


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
