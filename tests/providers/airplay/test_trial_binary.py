"""Private trial validation and owned-child attestation regressions."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import logging
import sys
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.helpers import private_trial
from music_assistant.helpers.process import AsyncProcess
from music_assistant.providers.airplay import helpers
from music_assistant.providers.airplay import trial_binary as trial
from music_assistant.providers.airplay.constants import StreamingProtocol
from music_assistant.providers.airplay.pairing import AirPlayPairing
from music_assistant.providers.airplay.stream import AirPlayStream
from tests.providers.airplay.test_provider import _make_provider
from tests.providers.airplay.test_stream import _make_player


def test_private_manifest_matches_source_pin() -> None:
    """Keep the private source overlay and launch record on the same accepted artifact."""
    manifest = json.loads(
        (Path(__file__).resolve().parents[3] / "private-native-airplay-trial-pin.json").read_text()
    )
    assert manifest["cli_sha256"] == trial.EXPECTED_SHA
    assert manifest["cli_build_label"] == trial.EXPECTED_BUILD
    assert manifest["cli_path"] == str(trial.TRIAL_ROOT / trial.EXPECTED_SHA / trial.ASSET_NAME)
    assert manifest["server_overlay_sha"] is None
    assert manifest["launch_authorized"] is False
    assert manifest["private_data_namespace"] == str(
        private_trial.APP_DATA / private_trial.NAMESPACE / "data"
    )
    assert manifest["private_cache_namespace"] == str(
        private_trial.APP_DATA / private_trial.NAMESPACE / "data" / ".cache"
    )
    assert manifest["private_marker_version"] == private_trial.VERSION
    assert manifest["external_receipt_required"] is True


def _elf() -> bytes:
    """Return a synthetic header which must NEVER execute on the host."""
    data = bytearray(64)
    data[:7] = b"\x7fELF\x02\x01\x01"
    data[16:18] = (2).to_bytes(2, "little")
    data[18:20] = (183).to_bytes(2, "little")
    data[20:24] = (1).to_bytes(4, "little")
    return bytes(data)


@pytest.fixture
def asset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin a synthetic ELF; only its check is mocked, never the asset validation."""
    root = tmp_path.resolve() / "media" / ".native-airplay"
    monkeypatch.setattr(trial, "TRIAL_ROOT", root)
    monkeypatch.setattr(trial, "EXPECTED_SHA", hashlib.sha256(_elf()).hexdigest())
    monkeypatch.setattr(trial, "is_trial_target", lambda: True)
    monkeypatch.setattr(trial, "_check_binary", AsyncMock(return_value=trial.EXPECTED_BUILD))
    path = root / trial.EXPECTED_SHA / trial.ASSET_NAME
    path.parent.mkdir(parents=True)
    path.write_bytes(_elf())
    path.chmod(0o755)
    return path


async def test_correct_pin_revalidated_every_time(asset: Path) -> None:
    """Mutable media never receives an indefinite successful validation cache."""
    assert await helpers.get_cli_binary() == str(asset)
    assert await helpers.get_cli_binary() == str(asset)
    asset.write_bytes(_elf() + b"changed")
    with pytest.raises(trial.TrialBinaryError, match="SHA256"):
        await helpers.get_cli_binary()


@pytest.mark.parametrize(
    "damage", ["missing", "digest", "architecture", "class", "endian", "nonexec", "large", "short"]
)
async def test_invalid_asset_never_executes(asset: Path, damage: str) -> None:
    """Reject unsafe bytes or mode before any --check or packaged-stock probe."""
    if damage == "missing":
        asset.unlink()
    elif damage == "nonexec":
        asset.chmod(0o644)
    elif damage == "large":
        with asset.open("r+b") as stream:
            stream.truncate(trial.MAX_BINARY_SIZE + 1)
    else:
        data = bytearray(_elf())
        if damage == "architecture":
            data[18] = 62
        elif damage == "class":
            data[4] = 1
        elif damage == "endian":
            data[5] = 2
        elif damage == "short":
            data = data[:10]
        else:
            data += b"wrong"
        asset.write_bytes(data)
    with patch.object(helpers, "check_output", new_callable=AsyncMock) as stock:
        with pytest.raises(trial.TrialBinaryError):
            await helpers.get_cli_binary()
        stock.assert_not_called()
    assert isinstance(trial._check_binary, AsyncMock)
    trial._check_binary.assert_not_called()


@pytest.mark.parametrize("parent", [False, True])
async def test_symlink_and_parent_escape_rejected(asset: Path, parent: bool) -> None:
    """Reject both executable symlinks and directory aliases outside the fixed tree."""
    target = asset.parent.parent / "outside"
    if parent:
        asset.parent.rename(target)
        asset.parent.symlink_to(target, target_is_directory=True)
    else:
        asset.rename(target)
        asset.symlink_to(target)
    with pytest.raises(trial.TrialBinaryError):
        await helpers.get_cli_binary()


async def test_dotdot_path_rejected(asset: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Even a source-time accidental escape cannot become a generic file API."""
    monkeypatch.setattr(trial, "TRIAL_ROOT", asset.parent / "..")
    with pytest.raises(trial.TrialBinaryError, match="Unsafe"):
        await helpers.get_cli_binary()


async def test_replacement_during_check_rejected(asset: Path) -> None:
    """Check completion cannot bless a changed media file."""

    async def replace(_path: Path) -> str:
        asset.write_bytes(_elf() + b"replacement")
        return trial.EXPECTED_BUILD

    with (
        patch.object(trial, "_check_binary", side_effect=replace),
        pytest.raises(trial.TrialBinaryError),
    ):
        await helpers.get_cli_binary()


@pytest.mark.parametrize(
    ("script", "error"),
    [
        ("print('cliairplay check v5b3dcc4')", None),
        ("print('cliairplay check v5b3dcc4'); raise SystemExit(1)", "failed"),
        ("print('cliairplay check v0000000')", "failed"),
        ("print('secret token=never-report')", "failed"),
        ("import time; time.sleep(10)", "timed out"),
        ("import os; os.write(1, b'x' * 1000000)", "limit"),
    ],
)
async def test_real_bounded_check(
    script: str, error: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise actual host children without ever executing a Linux ELF on macOS."""
    spawn = asyncio.create_subprocess_exec
    children: list[asyncio.subprocess.Process] = []

    async def host_spawn(*args: object, **_kwargs: object) -> asyncio.subprocess.Process:
        assert args == ("/fixed-test-binary", "--check")
        child = await spawn(
            sys.executable,
            "-c",
            script,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            limit=trial.MAX_CHECK_OUTPUT + 1,
        )
        children.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", host_spawn)
    monkeypatch.setattr(trial, "CHECK_TIMEOUT", 0.2)
    if error:
        with pytest.raises(trial.TrialBinaryError, match=error) as caught:
            await trial._check_binary(Path("/fixed-test-binary"))
        assert "secret" not in str(caught.value)
    else:
        assert await trial._check_binary(Path("/fixed-test-binary")) == trial.EXPECTED_BUILD
    assert children
    assert all(child.returncode is not None for child in children)


async def test_early_failure_precedes_registration_and_ntp_fallback(asset: Path) -> None:
    """Neither provider setup nor its restart catch disguises an invalid trial pin."""
    asset.unlink()
    provider = _make_provider()
    with patch.object(provider, "_set_pyatv_log_level") as setup:
        with pytest.raises(trial.TrialBinaryError):
            await provider.handle_async_init()
        setup.assert_not_called()
    with pytest.raises(trial.TrialBinaryError):
        await provider._start_ptp_daemon()


async def test_all_callers_select_same_pin_and_fail_closed(asset: Path) -> None:
    """PTP, stream and delayed pairing execution all converge on the validated helper."""
    provider = _make_provider()
    provider.dacp_id = "test"
    mass = cast("MagicMock", provider.mass)
    mass.streams.get_source_ip = AsyncMock(return_value="127.0.0.1")
    mass.create_task.side_effect = lambda coro: coro.close()
    with patch("music_assistant.providers.airplay.provider.AsyncProcess") as spawn:
        spawn.return_value.start = AsyncMock()
        await provider._start_ptp_daemon()
        assert spawn.call_args.args[0][0] == str(asset)
    stream = AirPlayStream(_make_player())
    assert (await stream._build_cli_args())[0] == str(asset)
    pairing = AirPlayPairing(
        "127.0.0.1",
        "test",
        StreamingProtocol.AIRPLAY2,
        logging.getLogger(__name__),
        device_id="test",
    )
    await pairing.start_pairing_session()
    with patch("music_assistant.providers.airplay.pairing.AsyncProcess") as spawn:
        spawn.return_value.start = AsyncMock()
        with patch("asyncio.create_task", side_effect=lambda coro: coro.close()):
            await pairing._start_pair_setup_process()
        assert spawn.call_args.args[0][0] == str(asset)
    pairing._pair_proc = None
    asset.write_bytes(_elf() + b"changed")
    for operation in (
        provider._start_ptp_daemon,
        stream._build_cli_args,
        pairing._start_pair_setup_process,
    ):
        with pytest.raises(trial.TrialBinaryError):
            await operation()


@pytest.fixture
def host_executable(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin the host's actual signed executable, without copying or altering its signature."""
    executable = Path("/bin/sleep").resolve()
    monkeypatch.setattr(trial, "EXPECTED_SHA", hashlib.sha256(executable.read_bytes()).hexdigest())
    return executable


async def test_real_owned_child_proc_unavailable(host_executable: Path, tmp_path: Path) -> None:
    """A real process on hosts without procfs is unverified, never matched by launch path."""
    child = AsyncProcess([str(host_executable), "10"])
    async with child:
        with patch.object(trial, "PROC_ROOT", tmp_path / "absent"):
            result = await trial.attest_owned_child(child, "stream")
        assert result == {
            "role": "stream",
            "outcome": "unverified",
            "reason": "procfs-unavailable-or-child-exited",
            "errno": errno.ENOENT,
        }
    assert await trial.attest_owned_child(child, "stream") == {
        "role": "stream",
        "outcome": "unverified",
        "reason": "not-live",
    }


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (
            OSError(errno.EACCES, "private detail"),
            {"reason": "procfs-access-denied", "errno": errno.EACCES},
        ),
        (
            OSError(errno.EPERM, "private detail"),
            {"reason": "procfs-access-denied", "errno": errno.EPERM},
        ),
        (OSError("private detail"), {"reason": "procfs-read-failed"}),
        (
            OSError(errno.EIO, "private detail"),
            {"reason": "procfs-read-failed", "errno": errno.EIO},
        ),
        (ValueError("private detail"), {"reason": "executable-unstable-or-ineligible"}),
        (
            trial.ChildAttestationChangedError("private detail"),
            {"reason": "child-changed-during-attestation"},
        ),
    ],
)
async def test_owned_child_attestation_safe_failures(
    host_executable: Path,
    failure: OSError | ValueError,
    expected: dict[str, int | str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Procfs failure diagnostics stay finite and exclude private exception detail."""
    child = AsyncProcess([str(host_executable), "10"])
    async with child:
        monkeypatch.setattr(trial, "_read_child_digest", MagicMock(side_effect=failure))
        result = await trial.attest_owned_child(child, "stream")
    assert result == {"role": "stream", "outcome": "unverified"} | expected
    assert "private detail" not in json.dumps(result)


@pytest.mark.parametrize("outcome", ["match", "mismatch", "exit", "race", "exec"])
async def test_owned_child_attestation(
    host_executable: Path, tmp_path: Path, outcome: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Use real owned children and synthetic procfs only where macOS lacks procfs."""
    child = AsyncProcess([str(host_executable), "10"])
    async with child:
        assert child.proc is not None
        proc_root = Path("/proc")
        if outcome == "exec" or not (proc_root / str(child.proc.pid) / "exe").exists():
            proc_root = tmp_path / "proc"
            directory = proc_root / str(child.proc.pid)
            directory.mkdir(parents=True)
            (directory / "exe").symlink_to(host_executable)
        monkeypatch.setattr(trial, "PROC_ROOT", proc_root)
        if outcome == "mismatch":
            monkeypatch.setattr(trial, "EXPECTED_SHA", "0" * 64)
        if outcome == "exit":
            await child.close()
        if outcome == "race":
            read = trial._read_child_digest
            loop = asyncio.get_running_loop()

            def exiting_read(pid: int) -> str:
                digest = read(pid)
                asyncio.run_coroutine_threadsafe(child.kill(), loop).result(timeout=2)
                return digest

            monkeypatch.setattr(trial, "_read_child_digest", exiting_read)
        if outcome == "exec":
            digest_fd = trial._digest_fd
            replacement = tmp_path / "replacement"
            replacement.write_bytes(b"different executable")
            exe_link = proc_root / str(child.proc.pid) / "exe"

            def replacing_digest(fd: int) -> tuple[str, bytes]:
                result = digest_fd(fd)
                exe_link.unlink()
                exe_link.symlink_to(replacement)
                return result

            monkeypatch.setattr(trial, "_digest_fd", replacing_digest)
        result = await trial.attest_owned_child(child, "stream")
        assert result["outcome"] == (
            "unverified" if outcome in ("exit", "race", "exec") else outcome
        )
        if outcome == "exit":
            assert result["reason"] == "not-live"
        elif outcome in ("race", "exec"):
            assert result["reason"] == "child-changed-during-attestation"
        assert "sleep" not in json.dumps(result)
        assert str(child.proc.pid) not in json.dumps(result)
        if outcome in ("mismatch", "exit", "race", "exec"):
            assert "actual_sha256" not in result


async def test_diagnostics_only_owned_roles_no_secrets(asset: Path) -> None:
    """Provider diagnostics expose no process args, env, credentials, IDs or paths."""
    provider = _make_provider()
    provider._dacp_server = MagicMock()
    provider._trial_identity = await trial.validate_trial_binary()
    player = _make_player()
    player.stream = MagicMock(running=False, _cli_proc=None)
    player._trial_pairing = None
    provider._ptp_daemon = AsyncProcess(["/secret/path", "token=private"])
    with patch.object(provider, "get_players", return_value=[player]):
        result = await provider.get_diagnostics()
    encoded = json.dumps(result["trial_binary"])
    assert "secret" not in encoded
    assert "private-official" in encoded
    assert str(asset) not in encoded
    identity = result["trial_binary"]
    assert isinstance(identity, dict)
    assert identity["owned_children"] == [
        {"role": "ptp", "outcome": "unverified", "reason": "not-live"}
    ]


@pytest.mark.parametrize("reason", ["Trial check failed", "Trial check timed out"])
async def test_check_failure_propagates_for_every_caller(asset: Path, reason: str) -> None:
    """No caller can route a bad/timed-out trial check to stock or NTP fallback."""
    provider = _make_provider()
    pairing = AirPlayPairing(
        "127.0.0.1",
        "test",
        StreamingProtocol.AIRPLAY2,
        logging.getLogger(__name__),
        device_id="test",
    )
    pairing._cli_binary = str(asset)
    stream = AirPlayStream(_make_player())
    with (
        patch.object(trial, "_check_binary", side_effect=trial.TrialBinaryError(reason)),
        patch.object(helpers, "check_output", new_callable=AsyncMock) as stock,
    ):
        for operation in (
            provider.handle_async_init,
            provider._start_ptp_daemon,
            pairing.start_pairing_session,
            pairing._start_pair_setup_process,
            stream._build_cli_args,
        ):
            with pytest.raises(trial.TrialBinaryError, match=reason):
                await operation()
        stock.assert_not_called()


async def test_diagnostics_attest_all_owned_roles(
    host_executable: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provider-owned live stream, PTP and pairing references get separate attestations."""
    child = AsyncProcess([str(host_executable), "10"])
    async with child:
        assert child.proc is not None
        proc_root = tmp_path / "proc"
        directory = proc_root / str(child.proc.pid)
        directory.mkdir(parents=True)
        (directory / "exe").symlink_to(host_executable)
        monkeypatch.setattr(trial, "PROC_ROOT", proc_root)
        monkeypatch.setattr(trial, "is_trial_target", lambda: True)
        provider = _make_provider()
        provider._dacp_server = MagicMock()
        provider._ptp_daemon = child
        player = _make_player()
        player.stream = MagicMock(running=False, _cli_proc=child)
        player._trial_pairing = MagicMock(_pair_proc=child)
        with patch.object(provider, "get_players", return_value=[player]):
            result = await provider.get_diagnostics()
        identity = result["trial_binary"]
        assert isinstance(identity, dict)
        expected_digest = private_trial.encode_public_digest(trial.EXPECTED_SHA)
        assert identity["owned_children"] == [
            {"role": role, "outcome": "match", "actual_sha256": expected_digest}
            for role in ("ptp", "stream", "pairing")
        ]
        encoded = json.dumps(identity)
        assert str(child.proc.pid) not in encoded
        assert str(host_executable) not in encoded
