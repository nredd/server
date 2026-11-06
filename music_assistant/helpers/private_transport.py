"""
Attempt ownership for the opt-in private finite-stimulus transport.

References:
    https://docs.python.org/3/library/asyncio-subprocess.html
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from contextvars import ContextVar
from typing import Any, Protocol
from urllib.parse import urlsplit

_TOKEN = re.compile(r"[0-9a-f]{32}")
_PLAYER = re.compile(r"ap[0-9a-f]{12}")
_STIMULUS_PATH = re.compile(r"/stimulus\.wav")
CURRENT_ATTEMPT: ContextVar[PrivateTransportAttempt | None] = ContextVar(
    "private_transport_attempt", default=None
)


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
            raise ValueError("Invalid private stimulus URI") from err
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


class PrivateTransportAttempt:
    """One irreversible epoch-bound start and its retained resource ownership."""

    def __init__(
        self,
        attempt_id: str,
        player: object,
        queue: object,
        media: str,
        current: Callable[[], bool],
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
        self.revoked = False
        self.consumed = False
        self.start_generation = 0
        self.reason: str | None = None
        self.outcome = "armed"
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

    async def spawn(self, process: OwnedProcess) -> None:
        """Retain the wrapper and shielded spawn even if the caller is cancelled."""
        try:
            self.check_current()
        except asyncio.CancelledError:
            # Validation may expire just as an async factory returns an unstarted
            # wrapper owning descriptors. Its close remains outside playback locks.
            task = asyncio.create_task(process.close())
            self.spawns.append((process, task))
            raise
        task = asyncio.create_task(process.start())
        self.spawns.append((process, task))
        await asyncio.shield(task)
        self.check_current()

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
        self.outcome = "starting"
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

    def snapshot(self) -> dict[str, str | int | bool | None]:
        """Return software ownership facts, never audio or physical cessation proof."""
        return {
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
            self.outcome = "active"
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
            self.outcome = "incomplete"

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
                self.outcome = "incomplete"
                raise result
        if self.loop.time() > self.observe_at:
            self.outcome = "incomplete"
        else:
            self.outcome = "forced-containment" if self.forced else "natural-cleanup"

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
            self.forced = True
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
        readers = [
            asyncio.create_task(self._read_pipe(pipe, reader if pipe is proc.stderr else None))
            for pipe in (proc.stdout, proc.stderr)
            if pipe is not None
        ]
        waiter = asyncio.create_task(proc.wait())
        reaped, _ = await asyncio.wait({waiter}, timeout=max(0, self.drain_at - self.loop.time()))
        if not reaped and proc.returncode is None:
            self.forced = True
            with suppress(ProcessLookupError):
                proc.kill()
        await waiter
        await asyncio.gather(*readers)
        if proc.returncode is not None and proc.returncode < 0:
            self.forced = True
        # The child is already reaped and both pipes reached EOF. This only
        # finalizes wrapper-owned descriptors; it is not the live-child stop bound.
        await process.close()
