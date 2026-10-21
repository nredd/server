"""
Developer-only offline encrypted C -> MA controller -> FIFO acceptance.

References:
    - airplay-cli/build/session-volume-contract.md
    - tests/providers/airplay/test_session_volume.py

Requires this source checkout and an explicitly supplied, already built C test
harness. Synthetic verified identity proves ONLY the offline contract, not native
UI binding or volume availability. Never used by the ordinary MA test suite.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import logging
import os
import sys
import tempfile
from functools import partial
from pathlib import Path
from typing import Never, TypedDict
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant.helpers.named_pipe import AsyncNamedPipeWriter
from music_assistant.helpers.process import AsyncProcess
from music_assistant.providers.airplay import session_volume
from tests.providers.airplay import test_session_volume
from tests.providers.airplay.test_session_volume import VolumeRig, make_rig

ROOT = Path(__file__).resolve().parents[1]
WITNESS = b"[STATUS] session_volume_harness committed=1 milli=610\n"
LIMIT = 32768


class Report(TypedDict):
    """Public metrics, deliberately excluding identity and raw diagnostics."""

    offline: bool
    native_ui_verified: bool
    committed_milli: int
    actuator_targets: list[int]
    actuator_actual: list[int]
    child_exit: int


class ArgumentParser(argparse.ArgumentParser):
    """Keep invalid arguments on the bounded JSON failure path."""

    def error(self, message: str) -> Never:
        """Reject arguments without emitting paths or user-supplied values."""
        raise ValueError("Invalid arguments")


class WitnessProcess(AsyncProcess):
    """Observe bytes while the REAL stream stdout reader remains their consumer."""

    def __init__(self, harness: Path, fifo: Path) -> None:
        """Prepare an owned local C child without starting it."""
        super().__init__(
            [str(harness), "--session-volume-harness", str(fifo)], stdout=True, stderr=True
        )
        self.output = bytearray()
        self.ready = asyncio.Event()
        self.witness = asyncio.Event()

    async def read(self, n: int) -> bytes:
        """Record bounded output without parsing or dispatching native requests."""
        chunk = await super().read(n)
        self.output.extend(chunk)
        if len(self.output) > LIMIT:
            raise ValueError("Excessive child output")
        if b"[STATUS] session_volume_transport ready=1 publication=0\n" in self.output:
            self.ready.set()
        if WITNESS in self.output:
            self.witness.set()
        return chunk


class Transactions:
    """Mux ONLY actuator transactions to the fixture's accepted-receipt seam."""

    def __init__(self, rig: VolumeRig, fifo: AsyncNamedPipeWriter) -> None:
        """Retain the real FIFO and capture control transactions before child exit."""
        self.rig = rig
        self.fifo = fifo
        self.controls: list[str] = []
        self.completed = asyncio.Event()
        self.actual: list[int | None] = []

    async def write(self, index: int, data: bytes) -> bool:
        """Send bind/state/complete/unbind through real atomic pipe writes."""
        command = data.decode()
        if "ACTION=STREAM_VOLUME_APPLY\n" in command:
            return await self.rig.write(index, data)
        if index != 0:
            raise ValueError("Unexpected follower control transaction")
        accepted = await self.fifo.write_atomic(data)
        if accepted:
            self.controls.append(command)
            if "ACTION=SESSION_VOLUME_COMPLETE\n" in command:
                self.actual = [stream.session_volume.confirmed_level for stream in self.rig.streams]
                self.completed.set()
        return accepted


class LogCapture(logging.Handler):
    """Retain bounded debug diagnostics for privacy assertions, never print them."""

    def __init__(self) -> None:
        """Initialize an empty bounded capture."""
        super().__init__(logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Fail closed rather than retaining unbounded diagnostics."""
        self.messages.append(record.getMessage())
        if sum(map(len, self.messages)) > LIMIT:
            raise ValueError("Excessive diagnostics")


def validate_harness(value: str) -> Path:
    """Require an explicit absolute existing executable, never build or install it."""
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("Harness must be absolute")
    path = path.resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError("Harness must be an existing executable")
    return path


def assert_source_imports() -> None:
    """Reject installed packages or a fixture imported from another checkout."""
    assert Path.cwd().resolve() == ROOT, "Run from the MA source root"
    assert Path(session_volume.__file__).resolve() == (
        ROOT / "music_assistant/providers/airplay/session_volume.py"
    )
    assert Path(test_session_volume.__file__).resolve() == (
        ROOT / "tests/providers/airplay/test_session_volume.py"
    )
    # A shared worktree venv or CI container is valid. The imported source
    # paths above establish provenance; interpreter placement does not.


async def drain_stderr(child: WitnessProcess) -> bytes:
    """Drain stderr concurrently with stdout, bounding retained diagnostics."""
    assert child.proc is not None
    assert child.proc.stderr is not None
    result = bytearray()
    while chunk := await child.proc.stderr.read(1024):
        result.extend(chunk)
        if len(result) > LIMIT:
            raise ValueError("Excessive child diagnostics")
    return bytes(result)


def verify_transactions(mux: Transactions) -> list[int]:
    """Require real group interpolation and actual, nonoptimistic completion."""
    targets = [
        int(
            dict(line.split("=", 1) for line in command.strip().splitlines())["STREAM_VOLUME_LEVEL"]
        )
        for command in mux.rig.commands
    ]
    assert len(targets) == 4
    assert targets[:2] == [60, 30]
    assert targets[2:] == [63, 35], "Group interpolation must preserve unequal balance"
    assert mux.actual == [61, 35]
    completions = [item for item in mux.controls if "ACTION=SESSION_VOLUME_COMPLETE" in item]
    assert len(completions) == 1
    assert "SESSION_VOLUME_RESULT=committed\n" in completions[0]
    assert "SESSION_VOLUME_KNOWN=1\nSESSION_VOLUME_MILLI=610\n" in completions[0]
    assert "SESSION_VOLUME_GENERATION=7\nSESSION_VOLUME_ORDINAL=1\n" in completions[0]
    states = [item for item in mux.controls if "ACTION=SESSION_VOLUME_STATE" in item]
    assert states
    assert "SESSION_VOLUME_KNOWN=1\nSESSION_VOLUME_MILLI=600\n" in states[0]
    assert not any("SESSION_VOLUME_MILLI=630\n" in item for item in mux.controls)
    return targets[2:]


async def wait_for_record(event: asyncio.Event, reader: asyncio.Task[None]) -> None:
    """Fail promptly if the real stdout reader ends before the required record."""
    waiter = asyncio.create_task(event.wait())
    try:
        await asyncio.wait((waiter, reader), return_when=asyncio.FIRST_COMPLETED)
        if not event.is_set():
            await reader
            raise RuntimeError("Child exited before required record")
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


async def exercise(harness: Path, directory: Path) -> Report:
    """Run the real encrypted cross-process case, cleaning every owned resource."""
    fifo = AsyncNamedPipeWriter(str(directory / "commands"))
    with patch.object(session_volume, "_GENERATIONS", itertools.count(7)):
        rig = make_rig(2)
        child = WitnessProcess(harness, Path(fifo.path))
        mux = Transactions(rig, fifo)
        reader: asyncio.Task[None] | None = None
        stderr: asyncio.Task[bytes] | None = None
        origin = rig.streams[0]
        try:
            # Fixture defaults are intentionally discarded: receipts must be obtained
            # AFTER assigning the real process, and support must arrive on real stdout.
            origin._cli_proc = child
            origin.session_volume.supported = False
            for index, stream in enumerate(rig.streams):
                stream.session_volume.confirmed_level = None
                stream.session_volume._receipt_snapshot = None
                stream.commands_pipe = MagicMock(
                    write_atomic=AsyncMock(side_effect=partial(mux.write, index))
                )
            await fifo.create()
            await child.start()
            stderr = asyncio.create_task(drain_stderr(child))
            reader = asyncio.create_task(origin._stdout_reader())
            origin._stdout_reader_task = reader
            await wait_for_record(child.ready, reader)
            assert origin.session_volume.supported
            assert origin.session_volume.generation == 7
            assert await fifo.wait_for_reader(2)
            await origin.session_volume.apply(60)
            await rig.streams[1].session_volume.apply(30)
            rig.clamp = 61
            assert await origin.session_volume.bind(b"P\x00R", b"", verified_identity=True)
            await wait_for_record(mux.completed, reader)
            targets = verify_transactions(mux)
            await wait_for_record(child.witness, reader)
            exit_code = await child.wait()
            await reader
            diagnostics = await stderr
            assert exit_code == 0
            for private in (b"500052", b"SESSION_VOLUME_OUTPUT_UID", b"P\x00R"):
                assert private not in child.output + diagnostics
            return {
                "offline": True,
                "native_ui_verified": False,
                "committed_milli": 610,
                "actuator_targets": targets,
                "actuator_actual": [61, 35],
                "child_exit": exit_code,
            }
        finally:
            tasks = [task for task in (reader, stderr) if task is not None]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await child.kill()
            finally:
                try:
                    await rig.close()
                finally:
                    await fifo.remove()


async def run(harness: Path, timeout: float = 10) -> Report:
    """Apply the deadline and privacy gate to the offline-only acceptance."""
    if not 0 < timeout <= 30:
        raise ValueError("Invalid timeout")
    assert_source_imports()
    capture = LogCapture()
    logger = logging.getLogger()
    previous = logger.level
    logger.addHandler(capture)
    logger.setLevel(logging.DEBUG)
    try:
        with tempfile.TemporaryDirectory(prefix="ma-volume-") as directory:
            async with asyncio.timeout(timeout):
                report = await exercise(harness, Path(directory))
        assert not any(
            private in message
            for message in capture.messages
            for private in ("500052", "SESSION_VOLUME_OUTPUT_UID", "P\x00R")
        )
        return report
    finally:
        logger.removeHandler(capture)
        logger.setLevel(previous)
        capture.close()


def main(argv: list[str] | None = None) -> int:
    """Emit only bounded public JSON; failures never echo raw exception contents."""
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--harness", required=True)
    parser.add_argument("--timeout", type=float, default=10)
    try:
        args = parser.parse_args(argv)
        report = asyncio.run(run(validate_harness(args.harness), args.timeout))
    except Exception as err:
        # The command boundary must not expose child diagnostics or private IPC
        # fields, including exceptions raised by the real controller/fixture.
        sys.stdout.write(json.dumps({"offline": True, "error": type(err).__name__}) + "\n")
        return 1
    sys.stdout.write(json.dumps(report, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
