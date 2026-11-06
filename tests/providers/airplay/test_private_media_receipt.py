"""
Closed private `@MEDIA1` parser, scanner, route mapping and v2 endpoint regressions.

All addresses are synthetic documentation addresses.

References:
    https://datatracker.ietf.org/doc/html/rfc5737
"""

from __future__ import annotations

import asyncio
import json
from types import MethodType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.helpers.private_transport import (
    MAX_MEDIA_RECORD,
    MAX_STATUS_LINE,
    MAX_STDOUT_TOTAL,
    MediaReceipt,
    MediaReceiptError,
    PrivateTransportAttempt,
    ReceiptScanner,
    media_qualifies,
    parse_media_record,
    resolved_media_route,
)
from music_assistant.providers.airplay import provider as provider_module
from music_assistant.providers.airplay.constants import (
    CONF_EXPERIMENTAL_HOMEPOD_REMOTE,
    CONF_PRIVATE_NATIVE_VOLUME_CAPTURE,
    StreamingProtocol,
)
from music_assistant.providers.airplay.provider import AirPlayProvider
from music_assistant.providers.airplay.stream import AirPlayStream
from tests.providers.airplay.test_stream import _make_player

TOKEN = "a" * 32
TARGET = "192.0.2.10"
BASE: dict[str, Any] = {
    "v": 1,
    "terminal": "stop",
    "route": "raop",
    "epoch": 1,
    "fresh_units": 3,
    "fresh_bytes": 384,
    "idle_units": 0,
    "backlog_units": 0,
    "fault": False,
    "overflow": False,
    "addr": TARGET,
    "port": 6001,
    "setup_port": 6001,
}


async def retire(attempt: PrivateTransportAttempt) -> None:
    """Join cleanup and retire the watchdog without waiting out the fixed deadlines."""
    await attempt.request_stop("fixture-end")
    attempt.watchdog.cancel()
    await asyncio.gather(attempt.watchdog, return_exceptions=True)


def record(**over: Any) -> bytes:
    """Return one closed record line, with overrides applied in the CLI's key order."""
    body = json.dumps(BASE | over, separators=(",", ":"))
    return b"@MEDIA1 " + body.encode() + b"\n"


def test_cli_documented_line_parses_exactly() -> None:
    """The README line from the CLI source tree is accepted verbatim."""
    line = (
        b'@MEDIA1 {"v":1,"terminal":"stop","route":"raop","epoch":1,"fresh_units":1,'
        b'"fresh_bytes":128,"idle_units":0,"backlog_units":0,"fault":false,"overflow":false,'
        b'"addr":"192.0.2.10","port":6001,"setup_port":6001}\n'
    )
    receipt = parse_media_record(line)
    assert (receipt.route, receipt.fresh_units, receipt.fresh_bytes) == ("raop", 1, 128)
    assert len(line) <= MAX_MEDIA_RECORD


def test_idle_and_backlog_counts_are_additional_not_invalid() -> None:
    """Exclusion counters never invalidate a qualifying record."""
    receipt = parse_media_record(record(idle_units=9, backlog_units=4))
    assert media_qualifies(receipt, target_address=TARGET, resolved_route="raop")


def test_no_fresh_unit_has_null_endpoint() -> None:
    """Zero fresh units parse only with the null endpoint and never qualify."""
    receipt = parse_media_record(record(fresh_units=0, fresh_bytes=0, addr=None, port=0))
    assert not media_qualifies(receipt, target_address=TARGET, resolved_route="raop")


@pytest.mark.parametrize(
    "line",
    [
        b"",
        b"@MEDIA1 {}\n",
        b"@MEDIA2 {}\n",
        b'@MEDIA1 {"v":1}\n',
        record()[:-1],
        record() + b"\n",
        record()[:-1] + b"\r\n",
        record().replace(b'"v"', b'"\\u0076"'),
        record().replace(b'"epoch":1', b'"epoch":1,"epoch":1'),
        record().replace(b'"port":6001,', b'"port":6001,"extra":1,'),
        record(v=True),
        record(v=2),
        record(v=1.0),
        record(epoch=True),
        record(epoch=2),
        record(epoch=-1),
        record(fresh_units=4294967296),
        record(fresh_units=1.0),
        record(fresh_units="3"),
        record(fresh_bytes=None),
        record(fault=0),
        record(overflow="false"),
        record(terminal="STOP"),
        record(terminal=None),
        record(route="tcp"),
        record(route=None),
        record(addr="192.000.2.10"),
        record(addr="::1"),
        record(addr=" 192.0.2.10"),
        record(addr="192.0.2.256"),
        record(addr="192.0.2.10\u00e9"),
        record(addr=1),
        record(port=65536),
        record(port=-1),
        record(setup_port=True),
        record(fresh_units=0, fresh_bytes=1),
        record(fresh_units=1, fresh_bytes=0),
        record(addr=None),
        record(fresh_units=0, fresh_bytes=0, addr=TARGET),
        record(fresh_units=0, fresh_bytes=0, addr=None, port=6001),
        record().replace(b'"fresh_units":3', b'"fresh_units":03'),
        record().replace(b'"fresh_units":3', b'"fresh_units":-0'),
        record().replace(b'"fresh_units":3', b'"fresh_units":3e0'),
        record().replace(b'"fault":false', b'"fault":NaN'),
        b"@MEDIA1 " + b"[" * 600 + b"\n",
        record().replace(b"}", b" " * 600 + b"}"),
    ],
)
def test_malformed_records_fail_closed(line: bytes) -> None:
    """Every shape violation is rejected without echoing child data."""
    with pytest.raises(MediaReceiptError) as err:
        parse_media_record(line)
    assert "192.0.2" not in str(err.value)


def _qualifying() -> MediaReceipt:
    """Return a record that satisfies every condition."""
    return parse_media_record(record())


@pytest.mark.parametrize(
    "over",
    [
        {"terminal": "abnormal"},
        {"route": "buffered"},
        {"route": "unknown"},
        {"route": "udp"},
        {"epoch": 0},
        {"fresh_units": 0, "fresh_bytes": 0, "addr": None, "port": 0},
        {"fault": True},
        {"overflow": True},
        {"addr": "192.0.2.11"},
        {"port": 6002},
        {"port": 0, "setup_port": 0},
    ],
)
def test_each_condition_is_necessary(over: dict[str, Any]) -> None:
    """Dropping any one qualifying condition makes the record non-qualifying."""
    assert media_qualifies(_qualifying(), target_address=TARGET, resolved_route="raop")
    receipt = parse_media_record(record(**over))
    assert not media_qualifies(receipt, target_address=TARGET, resolved_route="raop")


def test_route_must_match_independent_resolution() -> None:
    """A valid record never qualifies against another resolved route."""
    receipt = _qualifying()
    for other in ("compat", "udp", "buffered", "unknown", ""):
        assert not media_qualifies(receipt, target_address=TARGET, resolved_route=other)


@pytest.mark.parametrize(
    ("fields", "route"),
    [
        ({"protocol": "raop", "flow": "legacy", "buffered": "0"}, "raop"),
        ({"protocol": "airplay2", "flow": "raop-compat", "buffered": "0"}, "compat"),
        ({"protocol": "airplay2", "flow": "native", "buffered": "0"}, "udp"),
        ({"protocol": "airplay2", "flow": "native", "buffered": "1"}, "buffered"),
        ({"protocol": "airplay2", "flow": "raop-compat", "buffered": "1"}, "unknown"),
        ({"protocol": "raop", "flow": "native", "buffered": "0"}, "unknown"),
        ({"protocol": "airplay2", "flow": "legacy", "buffered": "0"}, "unknown"),
        ({"protocol": "airplay2"}, "unknown"),
        ({}, "unknown"),
    ],
)
def test_route_distinguishes_raop_compat_udp_buffered(fields: dict[str, str], route: str) -> None:
    """`[STATUS] route` facts map to the four actual routes, never broad `airplay2`."""
    assert resolved_media_route(fields) == route


@pytest.mark.parametrize(
    ("line", "route"),
    [
        ("[STATUS] route protocol=raop flow=legacy timing=ntp buffered=0", "raop"),
        ("[STATUS] route protocol=airplay2 flow=raop-compat timing=ntp buffered=0", "compat"),
        ("[STATUS] route protocol=airplay2 flow=native timing=ptp buffered=0", "udp"),
        ("[STATUS] route protocol=airplay2 flow=native timing=ptp buffered=1", "buffered"),
        ("[STATUS] route protocol=airplay2 flow=native timing=bad buffered=0", "unknown"),
        ("[STATUS] route protocol=airplay2 flow=native timing=ptp buffered=0 x=1", "unknown"),
    ],
)
def test_stream_parses_actual_route_and_latches_second_report(line: str, route: str) -> None:
    """The stream records the closed route once; a second report poisons it."""
    stream = AirPlayStream(_make_player())
    stream._parse_route_status(line)
    assert stream.private_media_route == route
    stream._parse_route_status(line)
    assert stream.private_media_route == "unknown"


def _scan(data: bytes, step: int) -> tuple[ReceiptScanner, list[str], MediaReceipt | None]:
    """Feed data in fixed steps and finish."""
    scanner = ReceiptScanner()
    lines: list[str] = []
    for start in range(0, len(data), step):
        lines += scanner.feed(data[start : start + step])
    return scanner, lines, scanner.finish()


@pytest.mark.parametrize("step", [1, 2, 3, 7, 64, 1024])
def test_scanner_is_split_invariant_and_diverts_media(step: int) -> None:
    """Ordinary lines pass; the record never appears in them at any split."""
    data = b"[STATUS] route a\n\n" + record() + b""
    scanner, lines, receipt = _scan(data, step)
    assert lines == ["[STATUS] route a"]
    assert receipt is not None
    assert receipt.fresh_units == 3
    assert not scanner.failed


@pytest.mark.parametrize(
    "data",
    [
        record() + record(),
        record() + b"[STATUS] late\n",
        record() + b"x",
        b"x " + record(),
        b"@MEDIA1 {bad\n",
        b"@MEDIA2 " + record()[8:],
        b"@MEDIA1 " + b"x" * MAX_MEDIA_RECORD + b"\n",
        b"y" * (MAX_STATUS_LINE + 1),
        record()[:-1],
        b"@MED",
        b"@MEDIA",
        b"",
    ],
)
def test_scanner_rejects_or_omits(data: bytes) -> None:
    """Duplicate, trailing, truncated, malformed and oversized input never yields a record."""
    _scanner, lines, receipt = _scan(data, 5)
    assert receipt is None
    assert all("@MEDIA" not in line for line in lines)


def test_scanner_total_cap_and_discard() -> None:
    """The 64 KiB total cap fails closed and then discards without retention."""
    scanner = ReceiptScanner()
    line = b"s" * 1000 + b"\n"
    seen = 0
    while not scanner.failed and seen < 200_000:
        scanner.feed(line)
        seen += len(line)
    assert scanner.failed
    assert MAX_STDOUT_TOTAL < seen <= MAX_STDOUT_TOTAL + 2 * len(line)
    assert scanner.feed(record()) == []
    assert scanner.finish() is None
    assert len(scanner._line) == 0


def test_status_line_at_cap_is_accepted() -> None:
    """A 4096-byte ordinary line (including LF) is the largest accepted."""
    _scanner, lines, _receipt = _scan(b"z" * (MAX_STATUS_LINE - 1) + b"\n", 100)
    assert len(lines) == 1


def _fake_provider(player: Any) -> Any:
    """Bind the real v1/v2 handlers onto a minimal provider double."""
    queues = SimpleNamespace(get=lambda _id: object(), play_media=AsyncMock())
    provider: Any = SimpleNamespace(
        _private_transport_attempt=None,
        _private_transport_epoch=-1,
        _private_admission_epoch=0,
        get_players=lambda: [player],
        _private_transport_current=lambda *_args: True,
        mass=SimpleNamespace(player_queues=queues),
    )
    for name in (
        "private_trial_transport",
        "private_trial_transport_v2",
        "_private_trial_transport",
    ):
        setattr(provider, name, MethodType(getattr(AirPlayProvider, name), provider))
    return provider


V1_KEYS = {
    "version",
    "attempt_id",
    "outcome",
    "revoked",
    "consumed",
    "start_generation",
    "reason",
    "cleanup_complete",
}


@pytest.fixture
def player_double() -> Any:
    """Return an admitted player double with a numeric IPv4 address."""
    return SimpleNamespace(player_id="ap0123456789ab", address=TARGET, stream=None)


async def _stop(provider: Any) -> None:
    """Retire the armed attempt's watchdog and cleanup."""
    attempt = provider._private_transport_attempt
    await retire(attempt)


def _arm(player: Any) -> dict[str, Any]:
    """Return a closed ARM request for the player double."""
    return {
        "mode": "arm",
        "attempt_id": TOKEN,
        "player_id": player.player_id,
        "media": "http://192.0.2.1:18080/stimulus.wav",
    }


async def test_v1_snapshot_is_byte_schema_unchanged(
    monkeypatch: pytest.MonkeyPatch, player_double: Any
) -> None:
    """The v1 command still returns exactly the eight v1 keys with version 1."""
    monkeypatch.setattr(provider_module, "is_trial_target", lambda: True)
    provider = _fake_provider(player_double)
    snapshot = await provider.private_trial_transport(_arm(player_double))
    assert set(snapshot) == V1_KEYS
    assert snapshot["version"] == 1
    assert provider._private_transport_attempt.version == 1
    assert provider._private_transport_attempt.target_address is None
    await _stop(provider)


async def test_v2_snapshot_adds_closed_pending_media(
    monkeypatch: pytest.MonkeyPatch, player_double: Any
) -> None:
    """The v2 command returns the v1 keys with version 2 plus the exact pending media."""
    monkeypatch.setattr(provider_module, "is_trial_target", lambda: True)
    provider = _fake_provider(player_double)
    snapshot = await provider.private_trial_transport_v2(_arm(player_double))
    assert set(snapshot) == V1_KEYS | {"media"}
    assert snapshot["version"] == 2
    assert snapshot["media"] == {
        "state": "pending",
        "route": "unknown",
        "epoch": None,
        "fresh_units": None,
        "fresh_bytes": None,
    }
    assert provider._private_transport_attempt.target_address == TARGET
    await _stop(provider)


@pytest.mark.parametrize(("armed", "other"), [(1, 2), (2, 1)])
@pytest.mark.parametrize("mode", ["get", "play", "stop", "arm"])
async def test_cross_version_calls_are_rejected(
    monkeypatch: pytest.MonkeyPatch, player_double: Any, armed: int, other: int, mode: str
) -> None:
    """The version is fixed at ARM; the other command can neither control nor re-arm."""
    monkeypatch.setattr(provider_module, "is_trial_target", lambda: True)
    provider = _fake_provider(player_double)
    handlers = {
        1: provider.private_trial_transport,
        2: provider.private_trial_transport_v2,
    }
    await handlers[armed](_arm(player_double))
    attempt = provider._private_transport_attempt
    request = _arm(player_double) if mode == "arm" else {"mode": mode, "attempt_id": TOKEN}
    with pytest.raises(ValueError, match=r"Unknown private transport attempt|already armed"):
        await handlers[other](request)
    assert provider._private_transport_attempt is attempt
    assert not attempt.consumed
    assert not attempt.revoked
    await _stop(provider)


@pytest.mark.parametrize("address", ["fe80::1", "192.000.2.10", "example.invalid", "", None])
async def test_v2_arm_requires_canonical_ipv4_target(
    monkeypatch: pytest.MonkeyPatch, player_double: Any, address: Any
) -> None:
    """A v2 attempt cannot compare an endpoint it cannot hold canonically."""
    monkeypatch.setattr(provider_module, "is_trial_target", lambda: True)
    player_double.address = address
    provider = _fake_provider(player_double)
    with pytest.raises(ValueError, match="numeric IPv4"):
        await provider.private_trial_transport_v2(_arm(player_double))
    assert provider._private_transport_attempt is None


@pytest.mark.parametrize("request_extra", [{"version": 2}, {"media_receipt": True}, {"extra": 1}])
async def test_v2_requests_keep_the_closed_v1_keys(
    monkeypatch: pytest.MonkeyPatch, player_double: Any, request_extra: dict[str, Any]
) -> None:
    """The version is chosen by command name, never by a request field."""
    monkeypatch.setattr(provider_module, "is_trial_target", lambda: True)
    provider = _fake_provider(player_double)
    with pytest.raises(ValueError, match="Invalid private"):
        await provider.private_trial_transport_v2(_arm(player_double) | request_extra)
    assert provider._private_transport_attempt is None


async def test_both_commands_are_registered() -> None:
    """The provider registers v1 unchanged and the new v2 command with SYSTEM_MANAGE."""
    provider: Any = MagicMock()
    provider._register_private_capture_api = AirPlayProvider._register_private_capture_api
    mass = provider.mass
    registered: list[tuple[str, Any, Any]] = []

    def register(name: str, handler: Any, **kw: Any) -> Any:
        """Record one registration and return its unregister."""
        registered.append((name, handler, kw))
        return lambda: None

    mass.register_api_command.side_effect = register
    with patch.object(provider_module, "is_trial_target", return_value=True):
        AirPlayProvider._register_private_capture_api(provider)
    names = {name: kw for name, _handler, kw in registered}
    assert "airplay/private_trial_transport" in names
    assert "airplay/private_trial_transport_v2" in names
    assert names["airplay/private_trial_transport_v2"] == names["airplay/private_trial_transport"]


@pytest.mark.parametrize(("version", "expected"), [(None, False), (1, False), (2, True)])
async def test_cli_flag_only_for_v2_attempts(version: int | None, expected: bool) -> None:
    """Only a v2 attempt passes the receipt flag; v1 and ordinary streams never do."""
    player = _make_player()
    stream: Any = AirPlayStream(player)
    stream.private_attempt = (
        None
        if version is None
        else PrivateTransportAttempt(TOKEN, player, object(), "x", lambda: True, version, TARGET)
    )
    stream.mass.streams.get_source_ip = AsyncMock(return_value=None)
    stream.mass.streams.get_publish_ip = MagicMock(return_value=None)
    with patch(
        "music_assistant.providers.airplay.stream.get_cli_binary",
        AsyncMock(return_value="/bin/cliairplay"),
    ):
        args = await stream._build_cli_args()
    assert (args.count("--private-media-receipt") == 1) is expected
    if expected:
        assert args.index("--private-media-receipt") < len(args) - 1
        assert args[-1] == player.address
    if stream.private_attempt is not None:
        await retire(stream.private_attempt)


async def test_v2_media_snapshot_invariants() -> None:
    """Pending and incomplete nulls, complete exactness, and no upgrade after incomplete."""
    player = SimpleNamespace(address=TARGET, stream=None)
    attempt = PrivateTransportAttempt(TOKEN, player, object(), "x", lambda: True, 2, TARGET)
    stream = object()
    attempt.bind_stream(stream)
    attempt.start_generation = 1
    assert attempt.media_snapshot()["state"] == "pending"
    receipt = parse_media_record(record())
    assert attempt.accept_media(receipt, stream=stream, resolved_route="raop")
    assert attempt.media_snapshot() == {
        "state": "complete",
        "route": "raop",
        "epoch": 1,
        "fresh_units": 3,
        "fresh_bytes": 384,
    }
    attempt.fail_media()
    assert attempt.media_snapshot()["state"] == "complete"
    other = PrivateTransportAttempt(TOKEN, player, object(), "x", lambda: True, 2, TARGET)
    other.bind_stream(stream)
    other.start_generation = 1
    other.fail_media()
    assert not other.accept_media(receipt, stream=stream, resolved_route="raop")
    assert other.media_snapshot() == {
        "state": "incomplete",
        "route": "unknown",
        "epoch": None,
        "fresh_units": None,
        "fresh_bytes": None,
    }
    for item in (attempt, other):
        await retire(item)


@pytest.mark.parametrize(
    "change",
    ["stream", "start", "address", "route", "late", "forced", "target"],
)
async def test_accept_media_conditions(change: str) -> None:
    """Each independently held condition is necessary for completion."""
    player = SimpleNamespace(address=TARGET, stream=None)
    attempt = PrivateTransportAttempt(TOKEN, player, object(), "x", lambda: True, 2, TARGET)
    stream = object()
    attempt.bind_stream(stream)
    attempt.start_generation = 1
    receipt = parse_media_record(record())
    route = "raop"
    if change == "stream":
        stream = object()
    elif change == "start":
        attempt.start_generation = 0
    elif change == "address":
        player.address = "192.0.2.99"
    elif change == "route":
        route = "compat"
    elif change == "late":
        attempt.observe_at = attempt.loop.time() - 0.001
    elif change == "forced":
        attempt._force()
    elif change == "target":
        attempt.target_address = None
    assert not attempt.accept_media(receipt, stream=stream, resolved_route=route)
    assert attempt.media_snapshot()["state"] == "incomplete"
    await retire(attempt)


async def test_pending_latches_incomplete_at_observation() -> None:
    """A missing record cannot stay pending past the immutable observation time."""
    player = SimpleNamespace(address=TARGET, stream=None)
    attempt = PrivateTransportAttempt(TOKEN, player, object(), "x", lambda: True, 2, TARGET)
    assert attempt.media_snapshot()["state"] == "pending"
    attempt.observe_at = attempt.loop.time() - 0.001
    assert attempt.media_snapshot()["state"] == "incomplete"
    await retire(attempt)


@pytest.mark.parametrize("version", [1, 2])
async def test_capture_eligible_connect_is_refused_only_for_v2(version: int) -> None:
    """A flag flip across connect's awaits cannot give a v2 receipt attempt native capture."""
    player = _make_player()
    player.protocol = StreamingProtocol.AIRPLAY2
    player.protocol_override = None
    stream: Any = AirPlayStream(player)
    stream.prov.config.get_value = lambda key, _default=None: (
        key
        in (
            CONF_EXPERIMENTAL_HOMEPOD_REMOTE,
            CONF_PRIVATE_NATIVE_VOLUME_CAPTURE,
        )
    )
    attempt = PrivateTransportAttempt(TOKEN, player, object(), "x", lambda: True, version, TARGET)
    stream.private_attempt = attempt
    process = AsyncMock(side_effect=RuntimeError("spawned"))
    with (
        patch(
            "music_assistant.providers.airplay.stream.trial_binary.is_trial_target", lambda: True
        ),
        patch.object(stream, "_build_cli_args", AsyncMock(return_value=["binary"])),
        patch.object(stream, "_create_private_pipe", AsyncMock()) as pipe,
        patch(
            "music_assistant.providers.airplay.stream.trial_binary.create_private_process",
            process,
        ),
    ):
        expected = ValueError if version == 2 else RuntimeError
        with pytest.raises(expected):
            await stream.connect()
    assert process.await_count == (0 if version == 2 else 1)
    assert pipe.await_count == (0 if version == 2 else 1)
    assert stream.native_volume_capture is None
    await retire(attempt)
