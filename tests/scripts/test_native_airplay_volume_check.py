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
import sys
from pathlib import Path
from types import ModuleType
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
    fifo = AsyncNamedPipeWriter(str(tmp_path / "commands"))
    mux = check.Transactions(rig, fifo)
    try:
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


async def test_process_timeout_reaps_child_and_removes_fifo(tmp_path: Path) -> None:
    """An unresponsive local executable cannot strand a child, reader or FIFO."""
    harness = tmp_path / "idle"
    harness.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
    harness.chmod(0o700)
    children: list[check.WitnessProcess] = []
    original = check.WitnessProcess

    def create(binary: Path, fifo: Path) -> check.WitnessProcess:
        """Retain ownership evidence without replacing the real process."""
        child = original(binary, fifo)
        children.append(child)
        return child

    with patch.object(check, "WitnessProcess", side_effect=create), pytest.raises(TimeoutError):
        async with asyncio.timeout(0.15):
            await check.exercise(harness, tmp_path)
    assert len(children) == 1
    assert children[0].closed
    assert children[0].returncode is not None
    assert not (tmp_path / "commands").exists()


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
