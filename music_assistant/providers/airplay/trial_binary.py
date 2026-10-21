"""
Private official DEV trial overlay, NEVER part of the upstream feature.

References:
    https://github.com/music-assistant/home-assistant-addon
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

from music_assistant.helpers.private_trial import is_trial_target as _is_trial_target
from music_assistant.helpers.process import get_subprocess_env

if TYPE_CHECKING:
    from music_assistant.helpers.json import SerializableType
    from music_assistant.helpers.process import AsyncProcess

EXPECTED_SHA = "078f71414f8a46811488fbbb8196ba8c1b60170cd19bc3c51c3819b8ee29279a"
EXPECTED_BUILD = "v5b3dcc4"
TRIAL_ROOT = Path("/media/.native-airplay")
ASSET_NAME = "cliairplay-linux-aarch64"
MAX_BINARY_SIZE = 64 * 1024 * 1024
MAX_CHECK_OUTPUT = 4096
CHECK_TIMEOUT = 5.0
PROC_ROOT = Path("/proc")
_BUILD_LABEL = re.compile(r"(?<![a-zA-Z0-9])v[0-9a-f]{7,40}(?![a-zA-Z0-9])")


class TrialBinaryError(RuntimeError):
    """Reject the private trial asset without stock or NTP fallback."""


def is_trial_target() -> bool:
    """Return whether this source overlay requires the fixed private trial pin."""
    return _is_trial_target()


async def validate_trial_binary() -> dict[str, SerializableType]:
    """Validate the fixed asset and its bounded check; return only safe identity fields."""
    path = TRIAL_ROOT / EXPECTED_SHA / ASSET_NAME
    try:
        digest = await asyncio.to_thread(_validate_asset)
        label = await _check_binary(path)
        # A check must not bless a file replaced while the child was running.
        if await asyncio.to_thread(_validate_asset) != digest:
            raise TrialBinaryError("Trial asset changed during validation")
    except OSError as err:
        raise TrialBinaryError("Trial asset unavailable or unsafe") from err
    return {
        "mode": "private-official-dev-trial",
        "expected_sha256": EXPECTED_SHA,
        "validated_sha256": digest,
        "check_label": label,
        "check_outcome": "passed",
    }


async def attest_owned_child(child: AsyncProcess, role: str) -> dict[str, SerializableType]:
    """Attest an owned process object, never a caller-supplied PID or executable path."""
    result: dict[str, SerializableType] = {"role": role, "outcome": "unverified"}
    proc = child.proc
    if proc is None or child.closed or proc.returncode is not None:
        return result
    try:
        digest = await asyncio.to_thread(_read_child_digest, proc.pid)
    except OSError, ValueError:
        return result
    if child.proc is not proc or child.closed or proc.returncode is not None:
        return result
    result["actual_sha256"] = digest
    result["outcome"] = "match" if digest == EXPECTED_SHA else "mismatch"
    return result


def _digest_fd(fd: int) -> tuple[str, bytes]:
    """Hash a bounded regular file descriptor and retain only its ELF header."""
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_BINARY_SIZE:
        raise ValueError("Trial executable is not a bounded regular file")
    digest = hashlib.sha256()
    total = 0
    header = b""
    while block := os.read(fd, min(65536, MAX_BINARY_SIZE + 1 - total)):
        if not header:
            header = block[:64]
        total += len(block)
        if total > MAX_BINARY_SIZE:
            raise ValueError("Trial executable exceeds size limit")
        digest.update(block)
    after = os.fstat(fd)
    if total != info.st_size or (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ) != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns):
        raise ValueError("Trial executable changed while reading")
    return digest.hexdigest(), header


def _validate_asset() -> str:
    """Open the fixed path without following symlinks, then validate bytes before execution."""
    path = TRIAL_ROOT / EXPECTED_SHA / ASSET_NAME
    if not path.is_absolute() or ".." in path.parts:
        raise TrialBinaryError("Unsafe fixed trial path")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        asset_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            if not os.fstat(asset_fd).st_mode & 0o111:
                raise TrialBinaryError("Trial asset is not executable")
            digest, header = _digest_fd(asset_fd)
        finally:
            os.close(asset_fd)
    except ValueError as err:
        raise TrialBinaryError("Invalid bounded trial asset") from err
    finally:
        os.close(fd)
    if (
        len(header) < 64
        or header[:7] != b"\x7fELF\x02\x01\x01"
        or int.from_bytes(header[18:20], "little") != 183
        or int.from_bytes(header[16:18], "little") not in (2, 3)
        or int.from_bytes(header[20:24], "little") != 1
    ):
        raise TrialBinaryError("Trial asset is not ELF64 little-endian AArch64")
    if digest != EXPECTED_SHA:
        raise TrialBinaryError("Trial asset SHA256 does not match the required pin")
    return digest


async def _check_binary(path: Path) -> str:
    """Run only the validated asset's check with bounded time and combined output."""
    proc = await asyncio.create_subprocess_exec(
        str(path),
        "--check",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=get_subprocess_env(),
        limit=MAX_CHECK_OUTPUT + 1,
    )
    try:
        async with asyncio.timeout(CHECK_TIMEOUT):
            assert proc.stdout is not None
            output = await proc.stdout.read(MAX_CHECK_OUTPUT + 1)
            if len(output) > MAX_CHECK_OUTPUT:
                raise TrialBinaryError("Trial check output exceeds limit")
            # read(n) may return a partial chunk; drain within the SAME bound.
            while block := await proc.stdout.read(MAX_CHECK_OUTPUT + 1 - len(output)):
                output += block
                if len(output) > MAX_CHECK_OUTPUT:
                    raise TrialBinaryError("Trial check output exceeds limit")
            await proc.wait()
        text = output.decode("ascii", errors="replace")
        labels = _BUILD_LABEL.findall(text)
        if (
            proc.returncode != 0
            or "cliairplay" not in text
            or "check" not in text
            or EXPECTED_BUILD not in labels
        ):
            raise TrialBinaryError("Trial check failed or reported the wrong build")
        return EXPECTED_BUILD
    except TimeoutError as err:
        raise TrialBinaryError("Trial check timed out") from err
    finally:
        if proc.returncode is None:
            with suppress(ProcessLookupError):
                proc.kill()
        # Discard, never accumulate, pipe bytes left after killing an over-limit check.
        async with asyncio.timeout(1.0):
            assert proc.stdout is not None
            while await proc.stdout.read(MAX_CHECK_OUTPUT):
                pass
            await proc.wait()


def _read_child_digest(pid: int) -> str:
    """Read only an owned child's proc executable with the same bounded hashing rule."""
    proc_fd = os.open(PROC_ROOT / str(pid), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open("exe", os.O_RDONLY | os.O_NONBLOCK, dir_fd=proc_fd)
        try:
            digest = _digest_fd(fd)[0]
            # A pinned proc directory prevents PID reuse from selecting another process.
            # Reopening its exe metadata rejects exit/zombie and concurrent exec races.
            current = os.stat("exe", dir_fd=proc_fd)
            original = os.fstat(fd)
            if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
                original.st_dev,
                original.st_ino,
                original.st_size,
                original.st_mtime_ns,
            ):
                raise ValueError("Owned executable changed during attestation")
            return digest
        finally:
            os.close(fd)
    finally:
        os.close(proc_fd)
