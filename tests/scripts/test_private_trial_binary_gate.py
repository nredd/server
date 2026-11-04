"""Regression coverage for the receiver-free private CLI artifact gate."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from music_assistant.providers.airplay import trial_binary
from scripts import private_trial_binary_gate as gate


def test_gate_import_does_not_require_the_server_dependency_stack() -> None:
    """The isolated HAOS probe imports the launcher/parser without loading the server."""
    source_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "from music_assistant.providers.airplay import trial_binary; "
                "assert 'music_assistant.mass' not in sys.modules"
            ),
        ],
        cwd=source_root,
        env={"PYTHONPATH": str(source_root)},
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr


async def test_gate_uses_provider_validation_then_current_image_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The standalone gate must retain the provider startup ordering and strict preflight."""
    calls: list[str] = []

    async def validate() -> dict[str, object]:
        calls.append("validate")
        return {}

    async def preflight() -> None:
        calls.append("preflight")

    monkeypatch.setattr(trial_binary, "validate_trial_binary", validate)
    monkeypatch.setattr(trial_binary, "preflight_current_image", preflight)

    await gate._run_gate()

    assert calls == ["validate", "preflight"]


def test_main_rejects_preflight_failure_without_claiming_check_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `--check`-passing but non-attesting candidate remains rejected."""
    monkeypatch.setattr(
        gate,
        "_run_gate",
        AsyncMock(side_effect=trial_binary.TrialBinaryError("acknowledgement is malformed")),
    )

    assert gate.main() == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "rejected" in captured.err
    assert "acknowledgement is malformed" in captured.err
