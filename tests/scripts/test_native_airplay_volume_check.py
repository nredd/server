"""
Unit and local-process tests for the explicit developer acceptance command.

References:
    - scripts/check_native_airplay_volume.py

No native binary, external checkout, receiver or network is required here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.helpers.named_pipe import AsyncNamedPipeWriter
from music_assistant.providers.airplay import session_volume
from scripts import check_native_airplay_volume as check
from tests.providers.airplay import test_session_volume
from tests.providers.airplay.test_session_volume import make_rig


@pytest.mark.parametrize("argv", [[], ["--harness", "relative"], ["--wrong", "secret"]])
def test_bad_arguments_are_private_json(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """Reject missing/invalid arguments without echoing supplied values."""
    assert check.main(argv) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"offline": True, "error": "ValueError"}
    assert captured.err == ""


def test_missing_and_nonexecutable_harness(tmp_path: Path) -> None:
    """Never install, build or accept a missing/nonexecutable native binary."""
    with pytest.raises(ValueError, match="existing executable"):
        check.validate_harness(str(tmp_path / "missing"))
    file = tmp_path / "not-executable"
    file.write_text("not a binary")
    with pytest.raises(ValueError, match="existing executable"):
        check.validate_harness(str(file))
    assert check.validate_harness(sys.executable) == Path(sys.executable).resolve()


def test_source_checkout_required(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An invocation from another directory cannot silently use installed packages."""
    check.assert_source_imports()
    monkeypatch.chdir(tmp_path)
    with pytest.raises(AssertionError, match="source root"):
        check.assert_source_imports()


@pytest.mark.parametrize("prefix", ["/usr/local", "/shared/developer-venv"])
def test_source_imports_accept_shared_and_container_interpreters(
    monkeypatch: pytest.MonkeyPatch, prefix: str
) -> None:
    """Import provenance depends on the checkout, not the interpreter's directory."""
    monkeypatch.setattr(sys, "prefix", prefix)
    check.assert_source_imports()


@pytest.mark.parametrize("module", [session_volume, test_session_volume])
def test_source_imports_reject_shadowed_modules(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, module: ModuleType
) -> None:
    """An unrelated installed module is rejected even with an otherwise valid source root."""
    monkeypatch.setattr(module, "__file__", str(tmp_path / "shadowed.py"))
    with pytest.raises(AssertionError):
        check.assert_source_imports()


@pytest.mark.parametrize("timeout", [0, -1, 31, float("nan")])
async def test_bad_timeout(timeout: float) -> None:
    """Reject invalid deadlines before spawning a process."""
    with pytest.raises(ValueError, match="timeout"):
        await check.run(Path(sys.executable), timeout)


async def test_mux_only_fakes_actuator_receipts(tmp_path: Path) -> None:
    """Control transactions reach the real FIFO; only checked actuators are faked."""
    rig = make_rig(1)
    try:
        fifo = AsyncNamedPipeWriter(str(tmp_path / "commands"))
        mux = check.Transactions(rig, fifo)
        with patch.object(fifo, "write_atomic", new_callable=AsyncMock) as write:
            write.return_value = True
            data = b"SESSION_VOLUME_GENERATION=7\nACTION=SESSION_VOLUME_UNBIND\n"
            assert await mux.write(0, data)
            write.assert_awaited_once_with(data)
            assert rig.commands == []
            with pytest.raises(ValueError, match="follower"):
                await mux.write(1, data)
    finally:
        await rig.close()


async def test_reader_failure_is_prompt() -> None:
    """EOF must fail immediately rather than waiting for the outer deadline."""
    reader = asyncio.create_task(asyncio.sleep(0))
    with pytest.raises(RuntimeError, match="required record"):
        await check.wait_for_record(asyncio.Event(), reader)


def _idle_harness(directory: Path) -> Path:
    """Write an executable that never answers."""
    harness = directory / "idle"
    harness.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
    harness.chmod(0o700)
    return harness


async def test_process_timeout_reaps_child_and_removes_fifo(tmp_path: Path) -> None:
    """An unresponsive local executable cannot strand a child, reader or FIFO."""
    harness = _idle_harness(tmp_path)
    children: list[check.WitnessProcess] = []
    started = asyncio.Event()

    class Observed(check.WitnessProcess):
        """Real witness process that reports when its spawn has completed."""

        def __init__(self, binary: Path, fifo: Path) -> None:
            """Retain ownership evidence without replacing the real process."""
            super().__init__(binary, fifo)
            children.append(self)

        async def start(self) -> None:
            """Spawn for real, then release the deadline."""
            await super().start()
            started.set()

    exercise: asyncio.Task[check.Report] | None = None
    try:
        with patch.object(check, "WitnessProcess", Observed):
            exercise = asyncio.create_task(check.exercise(harness, tmp_path))
            # Harness guard only. The 0.15s behavior deadline below starts once the child
            # exists: a deadline spanning spawn can cancel `create_subprocess_exec` before
            # `proc` is bound, leaving `returncode` None with nothing for the wrapper to reap.
            await asyncio.wait_for(started.wait(), 30)
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.15):
                    await exercise
    finally:
        if exercise is not None and not exercise.done():
            exercise.cancel()
        if exercise is not None:
            await asyncio.gather(exercise, return_exceptions=True)
    assert len(children) == 1
    assert children[0].closed
    assert children[0].returncode is not None
    assert not (tmp_path / "commands").exists()


async def test_cancel_during_spawn_leaks_no_child_or_fifo(tmp_path: Path) -> None:
    """A cancel landing after `Popen` but inside `create_subprocess_exec` leaks nothing."""
    harness = _idle_harness(tmp_path)
    children: list[check.WitnessProcess] = []
    spawned: list[subprocess.Popen[bytes]] = []
    popen_ran = asyncio.Event()

    class RecordingPopen(subprocess.Popen[bytes]):
        """Record the child the transport actually created."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            """Fork/exec for real, then report the owned child."""
            super().__init__(*args, **kwargs)
            spawned.append(self)
            popen_ran.set()

    class CancelledMidSpawn(check.WitnessProcess):
        """Real witness process whose spawn is cancelled once the child exists."""

        def __init__(self, binary: Path, fifo: Path) -> None:
            """Retain ownership evidence."""
            super().__init__(binary, fifo)
            children.append(self)

        async def start(self) -> None:
            """Cancel the real start after `Popen` ran, before the transport returned."""
            task = asyncio.create_task(super().start())
            await asyncio.wait_for(popen_ran.wait(), 30)
            task.cancel()
            await task

    with (
        patch.object(check, "WitnessProcess", CancelledMidSpawn),
        patch.object(subprocess, "Popen", RecordingPopen),
        pytest.raises(asyncio.CancelledError),
    ):
        await check.exercise(harness, tmp_path)
    assert len(children) == 1
    assert len(spawned) == 1
    # Exactly the state a deadline inside spawn produced: no wrapper-owned process object ...
    assert children[0].proc is None
    assert children[0].closed
    assert not (tmp_path / "commands").exists()
    # ... yet asyncio itself killed and reaped the exact child it had created.
    assert spawned[0].returncode is not None
    assert spawned[0].poll() is not None


async def test_early_child_failure_cleans_fifo(tmp_path: Path) -> None:
    """An actual child argument error fails without native fixtures or hardware."""
    with pytest.raises(RuntimeError, match="required record"):
        await check.exercise(Path(sys.executable), tmp_path)
    assert not (tmp_path / "commands").exists()


async def test_failure_restores_logging() -> None:
    """Deadline/failure restores debug capture state and keeps diagnostics private."""
    logger = logging.getLogger()
    level, handlers = logger.level, list(logger.handlers)
    with patch.object(check, "exercise", new_callable=AsyncMock) as exercise:
        exercise.side_effect = RuntimeError("private-output")
        with pytest.raises(RuntimeError, match="private-output"):
            await check.run(Path(sys.executable))
    assert logger.level == level
    assert logger.handlers == handlers


def test_main_sanitizes_failure(capsys: pytest.CaptureFixture[str]) -> None:
    """Do not echo process diagnostics, identities or exception contents."""
    with patch.object(check, "run", new_callable=AsyncMock) as run:
        run.side_effect = TimeoutError("private-output")
        assert check.main(["--harness", sys.executable]) == 1
    assert json.loads(capsys.readouterr().out) == {"offline": True, "error": "TimeoutError"}


def test_verifier_rejects_optimistic_or_broadcast_state() -> None:
    """The independent transaction gate cannot certify equal-level broadcasts."""
    mux = MagicMock()
    mux.rig.commands = [
        f"STREAM_VOLUME_LEVEL={level}\nACTION=STREAM_VOLUME_APPLY\n" for level in (60, 30, 63, 63)
    ]
    with pytest.raises(AssertionError, match="unequal balance"):
        check.verify_transactions(mux)
