"""
Run one bounded private TV RAOP transport experiment.

The controller deliberately uses Music Assistant's supported authenticated API.  It is
private trial tooling, not an AirPlay provider feature or receiver acceptance oracle.
It serves a deterministic finite WAV stimulus, performs one play request, and arms an
independent unconditional stop request within a nine-second monotonic budget.

References:
- https://music-assistant.io/
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import select
import signal
import struct
import sys
import threading
import time
import wave
from contextlib import suppress
from dataclasses import dataclass
from http import HTTPStatus
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

try:
    from scripts import private_dev_lifecycle as lifecycle
    from scripts.private_trial_scope import TrialMode, TrialScope, check_players
except ModuleNotFoundError:
    # The copied private tool is invoked as a script on the SSH add-on.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts import private_dev_lifecycle as lifecycle
    from scripts.private_trial_scope import TrialMode, TrialScope, check_players

SAMPLE_RATE = 44_100
CHANNELS = 2
SAMPLE_WIDTH = 2
TONE_SECONDS = 3
SILENCE_SECONDS = 1
AMPLITUDE = 10 ** (-45 / 20)
FREQUENCY_HZ = 440
HTTP_TIMEOUT_SECONDS = 3.0
# Observation window: the 4s wave plus 2s for MA's play latency, any metadata probe fetch,
# and receiver start. The final HTTP_TIMEOUT_SECONDS stay reserved for the stop request.
OBSERVATION_SECONDS = 6.0
STOP_BUDGET_SECONDS = OBSERVATION_SECONDS + HTTP_TIMEOUT_SECONDS
MAX_RESPONSE_BYTES = 64 * 1024


class TransportError(RuntimeError):
    """A bounded controller operation could not establish its required fact."""


class Api(Protocol):
    """Small authenticated Music Assistant API surface used by this controller."""

    def command(
        self, command: str, args: dict[str, Any], timeout: float = HTTP_TIMEOUT_SECONDS
    ) -> Any:
        """Call one supported Music Assistant JSON API command."""


class StopGuard(Protocol):
    """Independently dispatch a bounded stop before the transport deadline."""

    def arm(self, player: str, deadline: float) -> None:
        """Arm the stop path before playback is dispatched."""


@dataclass(frozen=True)
class Stimulus:
    """Exact finite WAV bytes served to the admitted Music Assistant instance."""

    payload: bytes
    sha256: str


class Receipt(BaseModel):
    """Finite result that separates observed facts from unproven receiver claims."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int
    outcome: str
    target_player: str
    stimulus_sha256: str
    stimulus_bytes: int
    completed_fetches: int
    ma_play_request_accepted: bool
    ma_stop_requested: bool
    child_route: str | None
    child_connected: bool | None
    child_pcm: bool | None
    limitations: list[str]
    error: str | None = None


class MusicAssistantApi:
    """Use MA's documented authenticated JSON endpoint without logging the credential."""

    def __init__(self, api_url: str, token: str) -> None:
        """Create a bounded authenticated client without exposing its token."""
        if not api_url.startswith(("http://", "https://")):
            raise TransportError("MA API URL must be HTTP(S)")
        if not token or any(character in token for character in "\r\n\x00"):
            raise TransportError("MA API credential is invalid")
        self._api_url = f"{api_url.rstrip('/')}/api"
        self._token = token

    def command(
        self, command: str, args: dict[str, Any], timeout: float = HTTP_TIMEOUT_SECONDS
    ) -> Any:
        """Make one deadline-bounded JSON request without retaining response credentials."""
        if not 0 < timeout <= HTTP_TIMEOUT_SECONDS:
            raise TransportError("MA API timeout is invalid")
        parsed_url = urlsplit(self._api_url)
        if not parsed_url.hostname or parsed_url.scheme not in {"http", "https"}:
            raise TransportError("MA API URL must be HTTP(S)")
        connection = (
            HTTPSConnection(parsed_url.hostname, parsed_url.port, timeout=timeout)
            if parsed_url.scheme == "https"
            else HTTPConnection(parsed_url.hostname, parsed_url.port, timeout=timeout)
        )
        body = json.dumps({"command": command, "args": args}, separators=(",", ":")).encode()
        try:
            with _AbsoluteTimeout(timeout):
                connection.request(
                    "POST",
                    parsed_url.path,
                    body=body,
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "Content-Type": "application/json",
                    },
                )
                response = connection.getresponse()
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if not 200 <= response.status < 300:
                    raise TransportError("MA API returned unsuccessful HTTP status")
        except (HTTPException, TimeoutError, OSError, ValueError) as err:
            raise TransportError("MA API request failed") from err
        finally:
            connection.close()
        if len(raw) > MAX_RESPONSE_BYTES:
            raise TransportError("MA API response exceeded limit")
        try:
            parsed = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            raise TransportError("MA API response was malformed") from err
        if isinstance(parsed, dict) and parsed.get("error"):
            raise TransportError("MA API rejected command")
        return parsed


def generate_stimulus() -> Stimulus:
    """Generate the exact 3-second -45dBFS tone followed by one second of silence."""
    frames = bytearray()
    tone_frames = SAMPLE_RATE * TONE_SECONDS
    total_frames = SAMPLE_RATE * (TONE_SECONDS + SILENCE_SECONDS)
    for index in range(total_frames):
        sample = (
            round(32767 * AMPLITUDE * math.sin(2 * math.pi * FREQUENCY_HZ * index / SAMPLE_RATE))
            if index < tone_frames
            else 0
        )
        frames.extend(struct.pack("<hh", sample, sample))
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(frames)
    payload = output.getvalue()
    return Stimulus(payload, hashlib.sha256(payload).hexdigest())


class StopWatchdog:
    """Detached, monotonic stop process that survives a blocked parent request."""

    def __init__(self, api_url: str, token: str, credential_env: str) -> None:
        """Keep the endpoint and credential only until the child is spawned."""
        self._api_url = api_url
        self._token = token
        self._credential_env = credential_env

    def arm(self, player: str, deadline: float) -> None:
        """Spawn and prove ready a stop child that shares the absolute deadline."""
        if deadline - time.monotonic() <= HTTP_TIMEOUT_SECONDS:
            raise TransportError("stop watchdog could not reserve its HTTP budget")
        ready_read, ready_write = os.pipe()
        try:
            argv = [
                sys.executable,
                str(Path(__file__).resolve()),
                "emergency-stop",
                "--ma-url",
                self._api_url,
                "--player",
                player,
                "--deadline",
                f"{deadline:.6f}",
                "--credential-env",
                self._credential_env,
                "--ready-fd",
                "198",
            ]
            os.posix_spawn(
                sys.executable,
                argv,
                {self._credential_env: self._token},
                setsid=True,
                file_actions=[
                    (os.POSIX_SPAWN_CLOSE, ready_read),
                    (os.POSIX_SPAWN_DUP2, ready_write, 198),
                    (os.POSIX_SPAWN_CLOSE, ready_write),
                ],
            )
            os.close(ready_write)
            ready_write = -1
            remaining = deadline - time.monotonic() - HTTP_TIMEOUT_SECONDS
            if remaining <= 0 or not select.select([ready_read], [], [], remaining)[0]:
                raise TransportError("stop watchdog did not become ready")
            if os.read(ready_read, 1) != b"1":
                raise TransportError("stop watchdog did not become ready")
        except OSError as err:
            raise TransportError("could not arm independent stop watchdog") from err
        finally:
            os.close(ready_read)
            if ready_write >= 0:
                os.close(ready_write)


class StimulusServer:
    """Serve only the exact generated bytes and retain a bounded fetch fact."""

    def __init__(self, bind: str, stimulus: Stimulus) -> None:
        """Bind an ephemeral private HTTP source for exactly one stimulus payload."""
        _ipv4(bind, "bind address")
        self._stimulus = stimulus
        self._completed_fetches = 0
        self._lock = threading.Lock()
        controller = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path != "/stimulus.wav":
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(controller._stimulus.payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(controller._stimulus.payload)
                controller._record_completed_fetch()

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self._server = ThreadingHTTPServer((bind, 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._started = False

    @property
    def port(self) -> int:
        """Return the assigned ephemeral port."""
        return int(self._server.server_port)

    @property
    def completed_fetches(self) -> int:
        """Return how many requests had the whole payload written to the socket."""
        with self._lock:
            return self._completed_fetches

    def start(self) -> None:
        """Start the bounded local source server."""
        self._thread.start()
        self._started = True

    def close(self) -> None:
        """Stop the source server and join its one owned thread."""
        if self._started:
            self._server.shutdown()
            self._thread.join(timeout=HTTP_TIMEOUT_SECONDS)
        self._server.server_close()

    def _record_completed_fetch(self) -> None:
        """Count one request whose full response body was handed to the socket."""
        with self._lock:
            self._completed_fetches += 1


def run_once(
    scope: TrialScope,
    api: Api,
    advertised_url: str,
    server: StimulusServer,
    stimulus: Stimulus,
    *,
    stop_guard: StopGuard | None = None,
) -> Receipt:
    """Run exactly one constrained MA playback request and unconditional bounded stop."""
    if scope.mode != TrialMode.TV_RAOP:
        raise TransportError("TV transport requires a tv-raop inventory")
    if not advertised_url.startswith("http://"):
        raise TransportError("stimulus URL must be explicit HTTP")
    receipt = Receipt(
        schema_version=2,
        outcome="INCOMPLETE",
        target_player=scope.player,
        stimulus_sha256=stimulus.sha256,
        stimulus_bytes=len(stimulus.payload),
        completed_fetches=0,
        ma_play_request_accepted=False,
        ma_stop_requested=False,
        child_route=None,
        child_connected=None,
        child_pcm=None,
        limitations=[
            "No native receiver group membership evidence.",
            "No acoustic or iPhone-control evidence.",
            "No RTP or packet evidence is produced or accepted by this tool.",
        ],
    )
    stop_deadline: float | None = None
    try:
        players = api.command("players/all", {})
        check_players(players, scope.player, TrialMode.TV_RAOP)
        # Begin and independently arm the stop budget before dispatch: MA may accept
        # a request whose response is lost or delayed, so parent cleanup is not safety.
        stop_deadline = time.monotonic() + STOP_BUDGET_SECONDS
        _require_stop_guard(stop_guard).arm(scope.player, stop_deadline)
        api.command(
            "player_queues/play_media",
            {"queue_id": scope.player, "media": advertised_url, "option": "replace"},
        )
        receipt.ma_play_request_accepted = True
        # Observe the whole window, however early a fetch lands: the first fetch may be
        # a metadata probe. Reserve a full bounded HTTP call for STOP so observing never
        # consumes the only interval in which the unconditional stop can be delivered.
        while time.monotonic() < stop_deadline - HTTP_TIMEOUT_SECONDS:
            time.sleep(0.02)
        # Fetches are source-server facts, never proof of receiver transport.
    except TransportError as err:
        receipt.error = str(err)
    finally:
        remaining = (stop_deadline - time.monotonic()) if stop_deadline is not None else 0
        if remaining > 0:
            try:
                api.command(
                    "players/cmd/stop",
                    {"player_id": scope.player},
                    min(remaining, HTTP_TIMEOUT_SECONDS),
                )
                receipt.ma_stop_requested = True
            except TransportError as err:
                receipt.error = receipt.error or str(err)
        elif stop_deadline is not None:
            receipt.error = receipt.error or "stop budget expired"
        receipt.completed_fetches = server.completed_fetches
    return receipt


class _DeadlineExpired(TimeoutError):
    """Raised when one synchronous HTTP operation outlives its absolute budget."""


def _raise_deadline(_signum: int, _frame: object) -> None:
    """Interrupt the in-flight main-thread socket operation at its hard deadline."""
    raise _DeadlineExpired


class _AbsoluteTimeout:
    """Apply one process-local monotonic deadline to all phases of a HTTP request."""

    def __init__(self, timeout: float) -> None:
        """Validate one short bounded operation timeout."""
        self._timeout = timeout
        self._previous_handler: Any = None
        self._previous_timer: tuple[float, float] | None = None

    def __enter__(self) -> None:
        """Install the temporary deadline timer only from the main thread."""
        if threading.current_thread() is not threading.main_thread():
            raise TransportError("MA API requests must run on the main thread")
        self._previous_handler = signal.signal(signal.SIGALRM, _raise_deadline)
        self._previous_timer = signal.setitimer(signal.ITIMER_REAL, self._timeout)

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        """Restore the caller's signal state after cancelling this short timer."""
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, self._previous_handler)
        if self._previous_timer is not None and self._previous_timer != (0.0, 0.0):
            signal.setitimer(signal.ITIMER_REAL, *self._previous_timer)


def _require_stop_guard(stop_guard: StopGuard | None) -> StopGuard:
    """Return the independently armed stop authority required before dispatch."""
    if stop_guard is None:
        raise TransportError("independent stop watchdog is required")
    return stop_guard


def _ipv4(value: str, label: str) -> str:
    """Validate one explicit unicast IPv4 address."""
    try:
        address = IPv4Address(value)
    except ValueError as err:
        raise TransportError(f"{label} must be an IPv4 address") from err
    if address.is_multicast or address.is_unspecified or int(address) == 0xFFFFFFFF:
        raise TransportError(f"{label} must be unicast")
    return str(address)


def validate_lifecycle_run(root: Path, run_id: str) -> None:
    """Require the existing independent DEV watchdog lease before transport dispatch."""
    run_dir, snapshot = lifecycle.load_snapshot(root, run_id)
    lifecycle._require_armed(root, run_dir, snapshot)


def _credential_from_env(name: str) -> str:
    """Read an inherited ephemeral credential without writing or displaying it."""
    if not name.isidentifier():
        raise TransportError("credential environment variable name is invalid")
    return os.environ.get(name, "")


def main(argv: list[str] | None = None) -> int:
    """Run the private transport controller once and emit only a sanitized receipt."""
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "emergency-stop":
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--ma-url", required=True)
        parser.add_argument("--player", required=True)
        parser.add_argument("--deadline", type=float, required=True)
        parser.add_argument("--credential-env", required=True)
        parser.add_argument("--ready-fd", type=int, required=True)
        args = parser.parse_args(argv[1:])
        if (
            not math.isfinite(args.deadline)
            or args.deadline - time.monotonic() <= HTTP_TIMEOUT_SECONDS
        ):
            return 1
        with suppress(OSError):
            os.write(args.ready_fd, b"1")
            os.close(args.ready_fd)
        stop_at = args.deadline - HTTP_TIMEOUT_SECONDS
        while (remaining := stop_at - time.monotonic()) > 0:
            time.sleep(min(remaining, 0.05))
        try:
            MusicAssistantApi(args.ma_url, _credential_from_env(args.credential_env)).command(
                "players/cmd/stop", {"player_id": args.player}
            )
        except TransportError:
            return 1
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--ma-url", required=True)
    parser.add_argument("--bind", required=True)
    parser.add_argument("--advertised-host", required=True)
    parser.add_argument("--credential-env", default="MA_TOKEN")
    parser.add_argument("--lifecycle-root", type=Path, required=True)
    parser.add_argument("--lifecycle-run", required=True)
    args = parser.parse_args(argv)
    server: StimulusServer | None = None
    try:
        scope = TrialScope.parse(json.loads(args.inventory.read_text(encoding="utf-8")))
        validate_lifecycle_run(args.lifecycle_root, args.lifecycle_run)
        bind = _ipv4(args.bind, "bind address")
        host = _ipv4(args.advertised_host, "advertised host")
        token = _credential_from_env(args.credential_env)
        api = MusicAssistantApi(args.ma_url, token)
        stop_guard = StopWatchdog(args.ma_url, token, args.credential_env)
        stimulus = generate_stimulus()
        server = StimulusServer(bind, stimulus)
        server.start()
        receipt = run_once(
            scope,
            api,
            f"http://{host}:{server.port}/stimulus.wav",
            server,
            stimulus,
            stop_guard=stop_guard,
        )
    except (OSError, ValueError, TransportError) as err:
        receipt = Receipt(
            schema_version=2,
            outcome="FAILED",
            target_player="unknown",
            stimulus_sha256="",
            stimulus_bytes=0,
            completed_fetches=0,
            ma_play_request_accepted=False,
            ma_stop_requested=False,
            child_route=None,
            child_connected=None,
            child_pcm=None,
            limitations=[],
            error=str(err),
        )
    finally:
        if server is not None:
            with suppress(OSError):
                server.close()
    sys.stdout.write(receipt.model_dump_json(exclude_none=False) + "\n")
    # This controller cannot establish receiver acceptance: clean source/stop facts are
    # deliberately incomplete, while a setup, request, or cleanup failure is failed.
    return 1 if receipt.outcome == "FAILED" or receipt.error or not receipt.ma_stop_requested else 2


if __name__ == "__main__":
    raise SystemExit(main())
