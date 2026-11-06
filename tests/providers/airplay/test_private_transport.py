"""
Receiver-free ownership, closed request and real-process deadline regressions.

References:
    https://docs.python.org/3/library/asyncio-subprocess.html
"""

from __future__ import annotations

import asyncio
import sys
import time
from types import SimpleNamespace
from typing import Any

import pytest

from music_assistant.helpers import process as process_module
from music_assistant.helpers.private_transport import PrivateTransportAttempt, validate_request
from music_assistant.helpers.process import AsyncProcess
from music_assistant.providers.airplay.stream import AirPlayStream

TOKEN = "a" * 32
URI = "http://192.0.2.1:18080/stimulus.wav"
ARM = {"mode": "arm", "attempt_id": TOKEN, "player_id": "ap021122334455", "media": URI}


@pytest.mark.parametrize("mode", ["arm", "play", "stop", "get"])
def test_closed_positive_requests(mode: str) -> None:
    """Each closed variant accepts exactly its defined members."""
    request = ARM if mode == "arm" else {"mode": mode, "attempt_id": TOKEN}
    assert validate_request(request) == mode


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"mode": True},
        {"mode": "unknown", "attempt_id": TOKEN},
        {"mode": "play", "attempt_id": True},
        {"mode": "play", "attempt_id": "A" * 32},
        {"mode": "play", "attempt_id": TOKEN, "media": URI},
        {"mode": "stop", "attempt_id": TOKEN, "option": "replace"},
        ARM | {"player_id": []},
        ARM | {"media": False},
        ARM | {"extra": 1},
    ],
)
def test_closed_request_refusals(payload: Any) -> None:
    """Unknown/malformed keys/types/tokens never reach a transport side effect."""
    with pytest.raises(ValueError, match="Invalid private"):
        validate_request(payload)


@pytest.mark.parametrize(
    "uri",
    [
        "https://192.0.2.1:18080/stimulus.wav",
        "http://example.invalid:18080/stimulus.wav",
        "http://127.0.0.1:18080/stimulus.wav",
        "http://0.0.0.0:18080/stimulus.wav",
        "http://224.0.0.1:18080/stimulus.wav",
        "http://256.0.0.1:18080/stimulus.wav",
        "http://192.0.2.1/stimulus.wav",
        "http://192.0.2.1:80/stimulus.wav",
        "http://user@192.0.2.1:18080/stimulus.wav",
        URI + "?next=http://example.invalid",
        URI + "#fragment",
        URI.replace("stimulus.wav", "other.wav"),
        "x" * 257,
    ],
)
def test_narrow_uri_refusals(uri: str) -> None:
    """Do not expand the private request into an arbitrary URL/options surface."""
    with pytest.raises(ValueError, match="Invalid private"):
        validate_request(ARM | {"media": uri})


async def wait_forever() -> None:
    """Hold an owned start operation without returning an Event's boolean result."""
    await asyncio.Event().wait()


def make_attempt() -> PrivateTransportAttempt:
    """Create one real-monotonic attempt with synthetic exact object identities."""
    return PrivateTransportAttempt(TOKEN, object(), object(), URI, lambda: True)


async def test_stop_is_irreversible_and_repeated_stop_joins() -> None:
    """Cancel the owned task synchronously before any asynchronous cleanup work."""
    attempt = make_attempt()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def operation() -> None:
        """Hold the real owned start task without a playback lock substitute."""
        entered.set()
        await release.wait()

    attempt.play(operation)
    await entered.wait()
    cleanup = attempt.request_stop("stop")
    assert attempt.revoked
    assert attempt.start_task is not None
    assert attempt.start_task.cancelling()
    assert attempt.request_stop("again") is cleanup
    with pytest.raises(asyncio.CancelledError):
        attempt.check_current()
    await cleanup
    await attempt.watchdog
    assert attempt.snapshot()["cleanup_complete"] is True
    assert attempt.outcome == "natural-cleanup"


async def test_expiry_does_not_depend_on_play_or_stop_response() -> None:
    """A lost ARM acknowledgement or absent PLAY leaves the original timer armed."""
    attempt = make_attempt()
    origin = time.monotonic()
    await attempt.watchdog
    assert 1.6 <= time.monotonic() - origin < 5
    assert attempt.revoked
    with pytest.raises(asyncio.CancelledError):
        attempt.play(wait_forever)


async def test_single_play_and_single_start_generation() -> None:
    """A duplicate cannot replace URI, restart the timer or issue another START."""
    attempt = make_attempt()
    original_deadline = attempt.cancel_at
    attempt.play(wait_forever)
    with pytest.raises(ValueError, match="consumed"):
        attempt.play(wait_forever)
    attempt.player = SimpleNamespace(stream=object())
    attempt.bind_stream(attempt.player.stream)
    assert attempt.claim_start()
    assert attempt.start_allowed()
    assert attempt.start_allowed()
    assert not attempt.claim_start()
    assert attempt.cancel_at == original_deadline
    await attempt.request_stop("test-end")
    await attempt.watchdog


class Child:
    """Actual receiver-free child, optionally delayed before returning its handle."""

    def __init__(self, code: str, release: asyncio.Event | None = None) -> None:
        """Keep the exact wrapper independently of the caller's cancelled task."""
        self.code = code
        self.release = release
        self.proc: asyncio.subprocess.Process | None = None

    async def close(self) -> None:
        """Verify the private primitive reaped before wrapper finalization."""
        assert self.proc is None or self.proc.returncode is not None

    async def start(self) -> None:
        """Start a real isolated Python process with no inherited environment."""
        if self.release is not None:
            await self.release.wait()
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-c",
            self.code,
            env={},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )


@pytest.mark.parametrize("forced", [False, True])
async def test_real_child_eof_or_forced_containment_and_reap(forced: bool) -> None:
    """Natural EOF and forced containment are observed and classified separately."""
    attempt = make_attempt()
    child = Child(
        "import time; time.sleep(30)" if forced else "import sys; sys.stdin.buffer.read()"
    )
    await attempt.spawn(child)
    assert child.proc is not None
    pid = child.proc.pid
    await attempt.request_stop("stop")
    await attempt.watchdog
    assert child.proc.pid == pid
    assert child.proc.returncode is not None
    assert child.proc.stdin is not None
    assert child.proc.stdin.is_closing()
    assert attempt.outcome == ("forced-containment" if forced else "natural-cleanup")
    assert time.monotonic() - attempt.armed_at < 5.1


async def test_actual_process_wrapper_eof_and_descriptor_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the actual production wrapper, not only the raw-child fixture."""
    monkeypatch.setattr(process_module, "get_subprocess_env", lambda _env: {})
    process = AsyncProcess(
        [sys.executable, "-I", "-c", "import sys; sys.stdin.buffer.read()"],
        stdin=True,
        stdout=True,
        stderr=True,
        env={},
    )
    attempt = make_attempt()
    await attempt.spawn(process)
    await attempt.request_stop("stop")
    await attempt.watchdog
    assert process.closed
    assert process.returncode == 0
    assert attempt.outcome == "natural-cleanup"
    assert attempt.snapshot()["cleanup_complete"] is True


async def test_late_spawn_retains_owner_and_cannot_pass_observation() -> None:
    """An unresolved process creation cannot pass or be forgotten at the deadline."""
    attempt = make_attempt()
    release = asyncio.Event()
    child = Child("import sys; sys.stdin.buffer.read()", release)
    caller = asyncio.create_task(attempt.spawn(child))
    await asyncio.sleep(0)
    caller.cancel()
    await asyncio.gather(caller, return_exceptions=True)
    cleanup = attempt.request_stop("stop")
    await attempt.watchdog
    assert attempt.outcome == "incomplete"
    assert not cleanup.done()
    assert not attempt.spawns[0][1].cancelled()
    release.set()
    await asyncio.wait_for(cleanup, 2)
    assert child.proc is not None
    assert child.proc.returncode is not None
    assert attempt.outcome == "incomplete"


async def test_release_failure_still_reaps_owned_child() -> None:
    """A failed resource detach cannot skip captured child cleanup."""
    attempt = make_attempt()
    child = Child("import sys; sys.stdin.buffer.read()")
    await attempt.spawn(child)

    async def failed_release() -> None:
        """Simulate an independent cleanup failure, not a successful stop."""
        raise OSError("synthetic detach failure")

    attempt.bind_session(object(), failed_release)
    with pytest.raises(OSError, match="synthetic"):
        await attempt.request_stop("stop")
    await attempt.watchdog
    assert child.proc is not None
    assert child.proc.returncode is not None
    assert attempt.outcome == "incomplete"


async def test_captured_stream_release_preserves_foreign_publication() -> None:
    """Old cleanup never looks up and clears a replacement stream."""
    foreign = object()
    stream: Any = SimpleNamespace(
        _stopping=False,
        _stopped=False,
        _teardown_started=asyncio.Event(),
        _stdout_reader_task=None,
        _cli_proc=None,
        commands_pipe=SimpleNamespace(remove=asyncio.sleep),
        player=SimpleNamespace(stream=foreign),
    )

    async def remove() -> None:
        """Remove only the captured fake FIFO."""

    stream.commands_pipe.remove = remove
    await AirPlayStream.private_release(stream)
    assert stream.player.stream is foreign
