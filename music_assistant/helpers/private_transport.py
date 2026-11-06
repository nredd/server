"""
Attempt ownership for the opt-in private finite-stimulus transport.

References:
    https://docs.python.org/3/library/asyncio-subprocess.html
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Protocol, TypedDict
from urllib.parse import urlsplit

_TOKEN = re.compile(r"[0-9a-f]{32}")
_PLAYER = re.compile(r"ap[0-9a-f]{12}")
_STIMULUS_PATH = re.compile(r"/stimulus\.wav")
CURRENT_ATTEMPT: ContextVar[PrivateTransportAttempt | None] = ContextVar(
    "private_transport_attempt", default=None
)


MEDIA_PREFIX: Final = b"@MEDIA1 "
MAX_MEDIA_RECORD: Final = 512
MAX_STATUS_LINE: Final = 4096
MAX_STDOUT_TOTAL: Final = 65536
MEDIA_ROUTES: Final = ("raop", "compat", "udp")
_MEDIA_KEYS: Final = frozenset(
    {
        "v",
        "terminal",
        "route",
        "epoch",
        "fresh_units",
        "fresh_bytes",
        "idle_units",
        "backlog_units",
        "fault",
        "overflow",
        "addr",
        "port",
        "setup_port",
    }
)
_ALL_ROUTES: Final = ("raop", "compat", "udp", "buffered", "unknown")
_CANONICAL_UINT = re.compile(r"0|[1-9][0-9]{0,9}")


class TransportOutcome(StrEnum):
    """Closed set of software ownership outcomes."""

    ARMED = "armed"
    STARTING = "starting"
    ACTIVE = "active"
    INCOMPLETE = "incomplete"
    FORCED_CONTAINMENT = "forced-containment"
    NATURAL_CLEANUP = "natural-cleanup"


class TransportSnapshot(TypedDict):
    """Software ownership facts for one attempt."""

    version: int
    attempt_id: str
    outcome: TransportOutcome
    revoked: bool
    consumed: bool
    start_generation: int
    reason: str | None
    cleanup_complete: bool


class MediaState(StrEnum):
    """Closed v2 sender-write evidence states; incomplete is terminal."""

    PENDING = "pending"
    INCOMPLETE = "incomplete"
    COMPLETE = "complete"


class MediaSnapshot(TypedDict):
    """Closed v2 media facts, sender-write evidence only."""

    state: MediaState
    route: str
    epoch: int | None
    fresh_units: int | None
    fresh_bytes: int | None


class TransportSnapshotV2(TransportSnapshot):
    """Exact v1 fields with `version` 2 plus the closed media object."""

    media: MediaSnapshot


class MediaReceiptError(ValueError):
    """Reject a private child media record; never carries raw child data."""


@dataclass(frozen=True)
class MediaReceipt:
    """One exactly parsed private `@MEDIA1` record."""

    terminal: str
    route: str
    epoch: int
    fresh_units: int
    fresh_bytes: int
    idle_units: int
    backlog_units: int
    fault: bool
    overflow: bool
    addr: str | None
    port: int
    setup_port: int


class OwnedProcess(Protocol):
    """Minimal process wrapper captured before its spawn task starts."""

    proc: asyncio.subprocess.Process | None
    _stderr_reader_task: asyncio.Task[None] | None

    async def start(self) -> None:
        """Start the captured wrapper."""

    async def close(self) -> None:
        """Release a wrapper that has not spawned a child."""


def validate_request(request: Any) -> str:
    """Validate the closed request shape without resolving or fetching its URI."""
    if type(request) is not dict or type(request.get("mode")) is not str:
        raise ValueError("Invalid private transport request")
    mode: str = request["mode"]
    expected = (
        {"mode", "attempt_id", "player_id", "media"} if mode == "arm" else {"mode", "attempt_id"}
    )
    if (
        mode not in ("arm", "play", "stop", "get")
        or set(request) != expected
        or type(request["attempt_id"]) is not str
        or _TOKEN.fullmatch(request["attempt_id"]) is None
    ):
        raise ValueError("Invalid private transport request")
    if mode == "arm":
        if (
            type(request["player_id"]) is not str
            or _PLAYER.fullmatch(request["player_id"]) is None
            or type(request["media"]) is not str
        ):
            raise ValueError("Invalid private transport target")
        media = request["media"]
        if len(media) > 256:
            raise ValueError("Invalid private stimulus URI")
        try:
            uri = urlsplit(media)
            port = uri.port
        except ValueError as err:
            diagnostic = "invalid port" if str(err).startswith("Port ") else "invalid authority"
            raise ValueError(f"Invalid private stimulus URI: {diagnostic}") from err
        # Numeric IPv4 only, no DNS, userinfo, redirects/options or arbitrary paths.
        host = uri.hostname or ""
        parts = host.split(".")
        if (
            media != f"http://{host}:{port}{uri.path}"
            or uri.scheme != "http"
            or len(parts) != 4
            or any(not x.isascii() or not x.isdigit() or str(int(x)) != x for x in parts)
            or any(int(x) > 255 for x in parts)
            or parts[0] in ("0", "127")
            or int(parts[0]) >= 224
            or port is None
            or not 1024 <= port <= 65535
            or uri.netloc != f"{host}:{port}"
            or _STIMULUS_PATH.fullmatch(uri.path) is None
            or uri.query
            or uri.fragment
        ):
            raise ValueError("Invalid private stimulus URI")
    return mode


def _reject_media_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate members instead of last-value-wins."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MediaReceiptError("Duplicate media member")
        result[key] = value
    return result


def _media_int(token: str) -> int:
    """Accept only canonical nonnegative decimal integers within u32."""
    if _CANONICAL_UINT.fullmatch(token) is None or int(token) > 0xFFFFFFFF:
        raise MediaReceiptError("Invalid media integer")
    return int(token)


def _media_reject(_token: str) -> object:
    """Reject floats and non-finite constants."""
    raise MediaReceiptError("Invalid media number")


def _media_uint(value: object, limit: int = 0xFFFFFFFF) -> int:
    """Return an exact (non-bool) integer within range."""
    if type(value) is not int or not 0 <= value <= limit:
        raise MediaReceiptError("Invalid media integer")
    return value


def _media_bool(value: object) -> bool:
    """Return an exact JSON boolean."""
    if type(value) is not bool:
        raise MediaReceiptError("Invalid media boolean")
    return value


def parse_media_record(line: bytes) -> MediaReceipt:
    """
    Parse one complete newline-terminated `@MEDIA1` line, exactly or not at all.

    :param line: The complete line including its prefix and trailing newline.
    :raises MediaReceiptError: If the line is not exactly one closed record.
    """
    if (
        len(line) > MAX_MEDIA_RECORD
        or not line.startswith(MEDIA_PREFIX)
        or line.count(b"\n") != 1
        or not line.endswith(b"\n")
        or b"\\" in line
        or b"\r" in line
    ):
        raise MediaReceiptError("Malformed media record")
    try:
        value = json.loads(
            line[len(MEDIA_PREFIX) : -1].decode("ascii"),
            object_pairs_hook=_reject_media_duplicates,
            parse_int=_media_int,
            parse_float=_media_reject,
            parse_constant=_media_reject,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as err:
        raise MediaReceiptError("Malformed media record") from err
    if type(value) is not dict or set(value) != _MEDIA_KEYS or value["v"] != 1:
        raise MediaReceiptError("Unexpected media members")
    if type(value["v"]) is not int:
        raise MediaReceiptError("Unexpected media version")
    addr = value["addr"]
    if addr is not None and (
        type(addr) is not str or not addr.isascii() or not is_canonical_ipv4(addr)
    ):
        raise MediaReceiptError("Invalid media address")
    terminal, route = value["terminal"], value["route"]
    if type(terminal) is not str or terminal not in ("stop", "abnormal"):
        raise MediaReceiptError("Invalid media terminal")
    if type(route) is not str or route not in _ALL_ROUTES:
        raise MediaReceiptError("Invalid media route")
    receipt = MediaReceipt(
        terminal=terminal,
        route=route,
        epoch=_media_uint(value["epoch"], 1),
        fresh_units=_media_uint(value["fresh_units"]),
        fresh_bytes=_media_uint(value["fresh_bytes"]),
        idle_units=_media_uint(value["idle_units"]),
        backlog_units=_media_uint(value["backlog_units"]),
        fault=_media_bool(value["fault"]),
        overflow=_media_bool(value["overflow"]),
        addr=addr,
        port=_media_uint(value["port"], 65535),
        setup_port=_media_uint(value["setup_port"], 65535),
    )
    if (
        (receipt.fresh_units == 0) != (receipt.fresh_bytes == 0)
        or (receipt.addr is None) != (receipt.fresh_units == 0)
        or (receipt.addr is None and receipt.port != 0)
    ):
        raise MediaReceiptError("Incompatible media fields")
    return receipt


def is_canonical_ipv4(text: str) -> bool:
    """Return whether text is canonical dotted-decimal IPv4."""
    try:
        return str(ipaddress.IPv4Address(text)) == text
    except ValueError:
        return False


def media_qualifies(receipt: MediaReceipt, *, target_address: str, resolved_route: str) -> bool:
    """
    Apply every necessary receipt condition against independently held facts.

    :param receipt: The parsed record.
    :param target_address: The canonical IPv4 address held independently of the child.
    :param resolved_route: The route parsed from the child's `[STATUS] route` line.
    """
    return (
        receipt.terminal == "stop"
        and receipt.route in MEDIA_ROUTES
        and receipt.route == resolved_route
        and receipt.epoch == 1
        and receipt.fresh_units >= 1
        and receipt.fresh_bytes >= 1
        and not receipt.fault
        and not receipt.overflow
        and receipt.addr == target_address
        and 1 <= receipt.port <= 65535
        and receipt.port == receipt.setup_port
    )


def resolved_media_route(fields: dict[str, str]) -> str:
    """
    Map the closed `[STATUS] route` facts to the receipt's route names; else unknown.

    :param fields: The `protocol`, `flow` and `buffered` values of the status line.
    """
    key = (fields.get("protocol", ""), fields.get("flow", ""), fields.get("buffered", ""))
    return {
        ("raop", "legacy", "0"): "raop",
        ("airplay2", "raop-compat", "0"): "compat",
        ("airplay2", "native", "0"): "udp",
        ("airplay2", "native", "1"): "buffered",
    }.get(key, "unknown")


class ReceiptScanner:
    """Bounded line splitter for private child stdout; diverts every media line."""

    def __init__(self) -> None:
        """Start with no line, no record and no failure."""
        self._line = bytearray()
        self._total = 0
        self._done = False
        self.record: MediaReceipt | None = None
        self.failed = False

    def feed(self, data: bytes) -> list[str]:
        """
        Return complete ordinary lines; media bytes never leave this object.

        :param data: The next screened stdout bytes.
        """
        lines: list[str] = []
        if self.failed:
            return lines
        self._total += len(data)
        pos = 0
        while pos < len(data) and not self.failed:
            if self._done or self._total > MAX_STDOUT_TOTAL:
                self.failed = True
                break
            newline = data.find(b"\n", pos)
            end = len(data) if newline < 0 else newline + 1
            self._line += data[pos:end]
            pos = end
            limit = MAX_MEDIA_RECORD if self._line[:6] == b"@MEDIA" else MAX_STATUS_LINE
            if len(self._line) > limit:
                self.failed = True
            elif newline >= 0:
                self._complete_line(lines)
        if self.failed:
            self._line.clear()
        return lines

    def finish(self) -> MediaReceipt | None:
        """At EOF return the single record, or None (failed, missing, or truncated)."""
        if self._line and (self._line.startswith(b"@MEDIA") or b"@MEDIA".startswith(self._line)):
            self.failed = True
        if self.failed or self.record is None:
            return None
        return self.record

    def _complete_line(self, lines: list[str]) -> None:
        """Classify one complete line; any media-looking line other than one record fails."""
        raw = bytes(self._line)
        self._line.clear()
        if b"@MEDIA" in raw:
            if self.record is not None or not raw.startswith(b"@MEDIA"):
                self.failed = True
                return
            try:
                self.record = parse_media_record(raw)
            except MediaReceiptError:
                self.failed = True
                return
            self._done = True
            return
        if text := raw.decode("utf-8", errors="replace").strip():
            lines.append(text)


class PrivateTransportAttempt:
    """One irreversible epoch-bound start and its retained resource ownership."""

    def __init__(
        self,
        attempt_id: str,
        player: object,
        queue: object,
        media: str,
        current: Callable[[], bool],
        version: int = 1,
        target_address: str | None = None,
    ) -> None:
        """Commit fixed server deadlines before any acknowledgement can be returned."""
        self.attempt_id = attempt_id
        self.player = player
        self.queue = queue
        self.media = media
        self.current = current
        self.loop = asyncio.get_running_loop()
        self.armed_at = self.loop.time()
        self.cancel_at = self.armed_at + 1.75
        self.drain_at = self.armed_at + 4.75
        self.observe_at = self.armed_at + 5.0
        self.version = version
        self.target_address = target_address
        self._media = MediaState.PENDING
        self._media_receipt: MediaReceipt | None = None
        self.stdout_owners: dict[OwnedProcess, asyncio.Task[None]] = {}
        self.revoked = False
        self.consumed = False
        self.start_generation = 0
        self.reason: str | None = None
        self.outcome = TransportOutcome.ARMED
        self.start_task: asyncio.Task[None] | None = None
        self.cleanup_task: asyncio.Task[None] | None = None
        self.session: object | None = None
        self.stream: object | None = None
        self.release: Callable[[], Awaitable[None]] | None = None
        self.spawns: list[tuple[OwnedProcess, asyncio.Task[None]]] = []
        self.forced = False
        self.readers_released = asyncio.Event()
        self.watchdog = asyncio.create_task(self._watchdog())

    def check_current(self) -> None:
        """Reject an expired or superseded owner before a side effect."""
        if self.revoked or self.loop.time() >= self.cancel_at or not self.current():
            self.request_stop("revoked")
            raise asyncio.CancelledError("Private transport revoked")

    def bind_session(self, session: object, release: Callable[[], Awaitable[None]]) -> None:
        """Capture one exact session before awaiting its start."""
        self.check_current()
        if self.session is not None:
            raise ValueError("Private transport already owns a session")
        self.session = session
        self.release = release

    def bind_stream(self, stream: object) -> None:
        """Capture one exact stream before publication or connection."""
        self.check_current()
        if self.stream is not None:
            raise ValueError("Private transport already owns a stream")
        self.stream = stream

    async def spawn(
        self,
        process: OwnedProcess,
        stdout_owner: Callable[[], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        """
        Retain the wrapper and shielded spawn even if the caller is cancelled.

        :param process: The captured wrapper to start.
        :param stdout_owner: Factory of the sole stdout reader, registered before spawn completes.
        """
        try:
            self.check_current()
        except asyncio.CancelledError:
            # Validation may expire just as an async factory returns an unstarted
            # wrapper owning descriptors. Its close remains outside playback locks.
            task = asyncio.create_task(process.close())
            self.spawns.append((process, task))
            raise
        task = asyncio.create_task(self._start(process, stdout_owner))
        self.spawns.append((process, task))
        await asyncio.shield(task)
        self.check_current()

    def fail_media(self) -> None:
        """Irreversibly mark media incomplete unless it already completed."""
        if self._media is MediaState.PENDING:
            self._media = MediaState.INCOMPLETE

    def accept_media(self, receipt: MediaReceipt, *, stream: object, resolved_route: str) -> bool:
        """
        Complete media once, only for the captured stream before observation.

        :param receipt: The record accepted at EOF.
        :param stream: The stream that owned stdout; must be the attempt's captured stream.
        :param resolved_route: The route parsed from the child's status line.
        """
        ok = (
            self._media is MediaState.PENDING
            and self.version == 2
            and stream is self.stream
            and not self.forced
            and self.loop.time() <= self.observe_at
            and self.start_generation == 1
            and self.target_address is not None
            and getattr(self.player, "address", None) == self.target_address
            and media_qualifies(
                receipt, target_address=self.target_address, resolved_route=resolved_route
            )
        )
        if not ok:
            self.fail_media()
            return False
        self._media = MediaState.COMPLETE
        self._media_receipt = receipt
        return True

    def media_snapshot(self) -> MediaSnapshot:
        """Return the closed media object; pending latches incomplete at observation."""
        if self._media is MediaState.PENDING and self.loop.time() >= self.observe_at:
            self._media = MediaState.INCOMPLETE
        receipt = self._media_receipt
        if self.forced or self._media is not MediaState.COMPLETE or receipt is None:
            pending = self._media is MediaState.PENDING and not self.forced
            return {
                "state": MediaState.PENDING if pending else MediaState.INCOMPLETE,
                "route": "unknown",
                "epoch": None,
                "fresh_units": None,
                "fresh_bytes": None,
            }
        return {
            "state": MediaState.COMPLETE,
            "route": receipt.route,
            "epoch": receipt.epoch,
            "fresh_units": receipt.fresh_units,
            "fresh_bytes": receipt.fresh_bytes,
        }

    def claim_start(self) -> bool:
        """Guard the actual FIFO delivery and consume exactly one START generation."""
        self.check_current()
        if self.start_generation:
            return False
        self.start_generation = 1
        return True

    def start_allowed(self) -> bool:
        """Recheck ownership at every FIFO delivery predicate without consuming again."""
        self.check_current()
        return self.start_generation == 1 and (
            self.stream is not None and getattr(self.player, "stream", None) is self.stream
        )

    def play(self, operation: Callable[[], Awaitable[None]]) -> None:
        """Consume PLAY once and retain its task independently of the HTTP response."""
        self.check_current()
        if self.consumed:
            raise ValueError("Private PLAY already consumed")
        self.consumed = True
        self.outcome = TransportOutcome.STARTING
        self.start_task = asyncio.create_task(self._play(operation))

    def invalidate(self, reason: str) -> None:
        """Irreversibly revoke before any lock wait or cancellation await."""
        self.revoked = True
        if self.reason is None:
            self.reason = reason
        if self.start_task is not None and not self.start_task.done():
            self.start_task.cancel()

    def request_stop(self, reason: str) -> asyncio.Task[None]:
        """Cancel before locks and join exactly one cleanup operation."""
        self.invalidate(reason)
        if self.cleanup_task is None:
            self.cleanup_task = asyncio.create_task(self._cleanup())
        return self.cleanup_task

    def snapshot(self) -> TransportSnapshot | TransportSnapshotV2:
        """Return software ownership facts, never audio or physical cessation proof."""
        base: TransportSnapshot = {
            "version": 1,
            "attempt_id": self.attempt_id,
            "outcome": self.outcome,
            "revoked": self.revoked,
            "consumed": self.consumed,
            "start_generation": self.start_generation,
            "reason": self.reason,
            "cleanup_complete": self.cleanup_task is not None
            and self.cleanup_task.done()
            and not self.cleanup_task.cancelled()
            and self.cleanup_task.exception() is None
            and all(
                task.done() and (process.proc is None or process.proc.returncode is not None)
                for process, task in self.spawns
            ),
        }
        if self.version != 2:
            return base
        return {**base, "version": 2, "media": self.media_snapshot()}

    async def _start(
        self, process: OwnedProcess, stdout_owner: Callable[[], Coroutine[Any, Any, None]] | None
    ) -> None:
        """Start the wrapper and register its sole stdout owner before spawn completes."""
        await process.start()
        if stdout_owner is not None:
            self.stdout_owners[process] = asyncio.create_task(stdout_owner())

    async def _play(self, operation: Callable[[], Awaitable[None]]) -> None:
        """Scope ambient ownership only to the real queue call."""
        token = CURRENT_ATTEMPT.set(self)
        try:
            self.check_current()
            await operation()
            self.check_current()
            if self.stream is not None and getattr(self.player, "stream", None) is not self.stream:
                self.request_stop("publication-changed")
                return
            self.outcome = TransportOutcome.ACTIVE
        except asyncio.CancelledError:
            self.request_stop("start-cancelled")
            raise
        except Exception:
            self.request_stop("start-failed")
        finally:
            CURRENT_ATTEMPT.reset(token)

    async def _watchdog(self) -> None:
        """Never rebase deadlines when PLAY or its acknowledgement is delayed."""
        await asyncio.sleep(max(0, self.cancel_at - self.loop.time()))
        cleanup = self.request_stop("expired")
        done, _ = await asyncio.wait({cleanup}, timeout=max(0, self.observe_at - self.loop.time()))
        if not done or cleanup.cancelled() or cleanup.exception() is not None:
            self.outcome = TransportOutcome.INCOMPLETE

    async def _cleanup(self) -> None:
        """Retain late spawns/reaps after the observation window without reporting success."""
        operations: list[Awaitable[Any]] = [
            self._drain(process, task) for process, task in self.spawns
        ]
        if self.start_task is not None:
            operations.append(asyncio.gather(self.start_task, return_exceptions=True))
        operations.append(self._release())
        results = await asyncio.gather(*operations, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                self.outcome = TransportOutcome.INCOMPLETE
                raise result
        self.media_snapshot()
        if self._media is MediaState.PENDING:
            self._media = MediaState.INCOMPLETE
        if self.loop.time() > self.observe_at:
            self.outcome = TransportOutcome.INCOMPLETE
        else:
            self.outcome = (
                TransportOutcome.FORCED_CONTAINMENT
                if self.forced
                else TransportOutcome.NATURAL_CLEANUP
            )

    def _force(self) -> None:
        """Record forced containment; media evidence can never survive it."""
        self.forced = True
        self.fail_media()

    async def _release(self) -> None:
        """Let captured readers relinquish pipes before raw drainers consume them."""
        try:
            if self.release is not None:
                await self.release()
        finally:
            self.readers_released.set()

    async def _read_pipe(
        self, pipe: asyncio.StreamReader, reader: asyncio.Task[None] | None
    ) -> None:
        """Drain only after both captured stream and process-owned readers stop."""
        await self.readers_released.wait()
        if reader is not None:
            await asyncio.gather(reader, return_exceptions=True)
        while await pipe.read(65536):
            pass

    async def _drain(self, process: OwnedProcess, spawn: asyncio.Task[None]) -> None:
        """Use one absolute window for EOF, forced containment and final reap."""
        done, _ = await asyncio.wait({spawn}, timeout=max(0, self.drain_at - self.loop.time()))
        if not done:
            self._force()
            if process.proc is not None and process.proc.returncode is None:
                with suppress(ProcessLookupError):
                    process.proc.kill()
        await asyncio.gather(spawn, return_exceptions=True)
        proc = process.proc
        if proc is None:
            return
        if proc.stdin is not None:
            proc.stdin.close()
        reader = process._stderr_reader_task
        if reader is not None:
            reader.cancel()
        owner = self.stdout_owners.get(process)
        readers = [
            asyncio.create_task(self._read_pipe(pipe, reader if pipe is proc.stderr else None))
            for pipe in (proc.stdout, proc.stderr)
            if pipe is not None and not (pipe is proc.stdout and owner is not None)
        ]
        waiter = asyncio.create_task(proc.wait())
        reaped, _ = await asyncio.wait({waiter}, timeout=max(0, self.drain_at - self.loop.time()))
        if not reaped and proc.returncode is None:
            self._force()
            with suppress(ProcessLookupError):
                proc.kill()
        await waiter
        await asyncio.gather(*readers)
        if owner is not None:
            outcome = (await asyncio.gather(owner, return_exceptions=True))[0]
            if outcome is not None:
                self.fail_media()
        if proc.returncode is not None and proc.returncode < 0:
            self._force()
        # The child is already reaped and both pipes reached EOF. This only
        # finalizes wrapper-owned descriptors; it is not the live-child stop bound.
        await process.close()
