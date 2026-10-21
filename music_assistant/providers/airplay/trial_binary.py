"""
Private official DEV trial overlay, NEVER part of the upstream feature.

References:
    https://github.com/music-assistant/home-assistant-addon
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import re
import secrets
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from music_assistant.helpers.private_trial import (
    encode_public_digest,
)
from music_assistant.helpers.private_trial import (
    is_trial_target as _is_trial_target,
)
from music_assistant.helpers.process import (
    AsyncProcess,
    descriptor_path,
    get_subprocess_env,
    open_in_thread,
)

if TYPE_CHECKING:
    from music_assistant.helpers.json import SerializableType

EXPECTED_SHA = "c65b4ddb067c85fafe461ab51eb9828e999eb0c7f3a7f87942a830cb35479199"
EXPECTED_BUILD = "v5c4901f"
TRIAL_ROOT = Path("/media/.native-airplay")
ASSET_NAME = "cliairplay-linux-aarch64"
MAX_BINARY_SIZE = 64 * 1024 * 1024
MAX_CHECK_OUTPUT = 4096
CHECK_TIMEOUT = 5.0
ATTEST_TIMEOUT = 5
MAX_ATTEST_LINE = 1024
PROC_ROOT = Path("/proc")
_BUILD_LABEL = re.compile(r"(?<![a-zA-Z0-9])v[0-9a-f]{7,40}(?![a-zA-Z0-9])")


class TrialBinaryError(RuntimeError):
    """Reject the private trial asset without stock or NTP fallback."""


class ChildAttestationChangedError(ValueError):
    """Reject an owned child whose executable changed during attestation."""


@dataclass(frozen=True)
class _TrustedChildReceipt:
    """Point-in-time acknowledgement bound to one exact owned process instance."""

    process: TrialAsyncProcess
    proc: asyncio.subprocess.Process
    digest: str


class TrialAsyncProcess(AsyncProcess):
    """Private CLI process that cannot expose readiness before its image acknowledgement."""

    def __init__(
        self,
        args: list[str],
        *,
        asset_fd: int,
        attest_only: bool = False,
        stdin: bool | int | None = None,
        stdout: bool | int | None = None,
        stderr: bool | int | None = False,
        name: str | None = None,
        env: dict[str, str] | None = None,
        pass_fds: tuple[int, ...] = (),
    ) -> None:
        """Initialize a process with one parent-owned executable descriptor."""
        self._asset_fd: int | None = asset_fd
        self._attest_nonce = secrets.token_hex(16)
        self._attest_only = attest_only
        self._stdout_pending = b""
        self._trusted_receipt: _TrustedChildReceipt | None = None
        self._exit_watcher: asyncio.Task[None] | None = None
        super().__init__(
            [
                descriptor_path(asset_fd),
                *args[1:],
                "--attest-fd",
                str(asset_fd),
                "--attest-nonce",
                self._attest_nonce,
            ],
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            name=name,
            env=env,
            pass_fds=(*pass_fds, asset_fd),
        )

    async def start(self) -> None:
        """Start the passed descriptor and consume its required first stdout line."""
        existing_proc = self.proc
        if existing_proc is not None or self._asset_fd is None:
            raise TrialBinaryError("Trial process launch cannot be reused")
        try:
            await super().start()
            assert self.proc is not None
            self._exit_watcher = asyncio.create_task(self._release_after_exit(self.proc))
            await self._verify_acknowledgement()
        except BaseException:
            await self._kill_and_reap()
            raise

    def verified_stdin_fd(self) -> int:
        """Export the owned input pipe only after this live child has acknowledged."""
        proc = self.proc
        if self._current_receipt() is None or proc is None or proc.stdin is None:
            raise TrialBinaryError("Trial input descriptor is blocked before image acknowledgement")
        if proc.stdin.is_closing():
            raise TrialBinaryError("Trial input descriptor is already closing")
        pipe = proc.stdin.transport.get_extra_info("pipe")
        if pipe is None:
            raise TrialBinaryError("Trial input descriptor is unavailable")
        return int(pipe.fileno())

    async def write(self, data: bytes) -> None:
        """Reject audio or pairing input until this exact live child has acknowledged."""
        if self._current_receipt() is None:
            raise TrialBinaryError("Trial input is blocked before image acknowledgement")
        await super().write(data)

    async def read(self, n: int) -> bytes:
        """Apply the same duplicate-frame gate to chunk consumers as line consumers."""
        if n == 0:
            return b""
        if n < 0:
            chunks: list[bytes] = []
            while chunk := await self.read_stdout():
                chunks.append(chunk)
            return b"".join(chunks)
        if not self._stdout_pending:
            self._stdout_pending = await self.read_stdout()
        result = self._stdout_pending[:n]
        self._stdout_pending = self._stdout_pending[n:]
        return result

    async def readexactly(self, n: int) -> bytes:
        """Keep exact-size stdout consumers behind the same duplicate-frame gate."""
        if n < 0:
            raise ValueError("Private stdout read size must not be negative")
        result = bytearray()
        while len(result) < n:
            block = await self.read(n - len(result))
            if not block:
                break
            result += block
        return bytes(result)

    async def communicate(self, *_args: object, **_kwargs: object) -> tuple[bytes, bytes]:
        """Reject raw communication which would bypass the private proof reader/writer."""
        raise TrialBinaryError("Private CLI communication requires verified stream methods")

    async def read_stdout(self) -> bytes:
        """Reject any second acknowledgement before either reader API can expose it."""
        if self._stdout_pending:
            line, self._stdout_pending = self._stdout_pending, b""
            return line
        if self._close_called and not self._stdout_left_to_reader:
            return b""
        if self.proc is None or self.proc.stdout is None:
            raise TrialBinaryError("Private stdout pipe is unavailable")
        try:
            async with self._stdout_lock:
                line = await self.proc.stdout.readline()
        except ValueError as err:
            self._trusted_receipt = None
            await self._kill_and_reap()
            raise TrialBinaryError("Private stdout line exceeds its limit") from err
        if line.startswith(b"[ATTEST]"):
            self._trusted_receipt = None
            await self._kill_and_reap()
            raise TrialBinaryError("Trial image acknowledgement was repeated")
        return line

    async def close(self) -> None:
        """Close the child and its parent-owned executable descriptor."""
        try:
            await super().close()
        finally:
            self._release_asset_fd()

    async def kill(self) -> None:
        """Kill the child and close its parent-owned executable descriptor."""
        try:
            await super().kill()
        finally:
            self._release_asset_fd()

    async def _verify_acknowledgement(self) -> None:
        """Accept one bounded acknowledgement before any caller can read stdout status."""
        try:
            async with asyncio.timeout(ATTEST_TIMEOUT):
                line = await self._read_attestation_line()
        except TimeoutError as err:
            raise TrialBinaryError("Trial image acknowledgement timed out") from err
        ack = _parse_attestation_line(line, self._attest_nonce)
        proc = self.proc
        if self._attest_only:
            if proc is None:
                raise TrialBinaryError("Trial image preflight process is unavailable")
            if await self.wait_with_timeout(ATTEST_TIMEOUT) != 0:
                raise TrialBinaryError("Trial image preflight did not exit successfully")
            return
        if proc is None or self.closed or proc.returncode is not None:
            raise TrialBinaryError("Trial image acknowledgement child exited")
        self._trusted_receipt = _TrustedChildReceipt(self, proc, ack)

    async def _read_attestation_line(self) -> bytes:
        """Read exactly one bounded first stdout line without consuming later status bytes."""
        if self.proc is None or self.proc.stdout is None:
            raise TrialBinaryError("Trial image acknowledgement pipe is unavailable")
        line = bytearray()
        async with self._stdout_lock:
            while len(line) <= MAX_ATTEST_LINE:
                chunk = await self.proc.stdout.read(1)
                if not chunk:
                    break
                line += chunk
                if chunk == b"\n":
                    return bytes(line)
        raise TrialBinaryError("Trial image acknowledgement is malformed")

    async def _kill_and_reap(self) -> None:
        """Bound rejection cleanup while preserving the gate failure that caused it."""
        try:
            if self.proc is not None and self.returncode is None:
                with suppress(ProcessLookupError):
                    self.proc.kill()
                await asyncio.wait_for(self.kill(), timeout=1.0)
        except (OSError, RuntimeError, TimeoutError) as err:
            self.logger.warning(f"Could not reap rejected private CLI child: '{err}'")
        finally:
            self._release_asset_fd()

    async def _release_after_exit(self, proc: asyncio.subprocess.Process) -> None:
        """Release the descriptor on natural exit even when a caller only polls closed."""
        try:
            await proc.wait()
        finally:
            if proc.returncode is not None:
                self._release_asset_fd()

    def _release_asset_fd(self) -> None:
        """Release the retained descriptor exactly once before its number can be reused."""
        if (asset_fd := self._asset_fd) is None:
            return
        self._asset_fd = None
        os.close(asset_fd)

    def _current_receipt(self) -> _TrustedChildReceipt | None:
        """Return the bound receipt only while its exact owned process remains live."""
        receipt = self._trusted_receipt
        if (
            receipt is None
            or receipt.process is not self
            or receipt.proc is not self.proc
            or self.closed
            or self.returncode is not None
        ):
            return None
        return receipt


def _parse_attestation_line(line: bytes, nonce: str) -> str:
    """Validate the exact JSON acknowledgement shape and return its pinned digest."""
    if len(line) > MAX_ATTEST_LINE or not line.endswith(b"\n"):
        raise TrialBinaryError("Trial image acknowledgement is malformed")
    try:
        prefix, payload = line.decode("ascii").rstrip("\n").split(" ", 1)
        ack = json.loads(payload, object_pairs_hook=_reject_duplicate_json_keys)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as err:
        raise TrialBinaryError("Trial image acknowledgement is malformed") from err
    if (
        prefix != "[ATTEST]"
        or not isinstance(ack, dict)
        or type(ack.get("version")) is not int
        or ack
        != {
            "version": 1,
            "kind": "current-image-match-at-ack",
            "nonce": nonce,
            "exec_fd_sha256": EXPECTED_SHA,
            "self_sha256": EXPECTED_SHA,
        }
    ):
        raise TrialBinaryError("Trial image acknowledgement did not match the pinned asset")
    return EXPECTED_SHA


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate keys instead of allowing JSON's last-value-wins behavior."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate acknowledgement key")
        result[key] = value
    return result


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
        "expected_sha256": encode_public_digest(EXPECTED_SHA),
        "validated_sha256": encode_public_digest(digest),
        "check_label": label,
        "check_outcome": "passed",
    }


async def attest_owned_child(child: AsyncProcess, role: str) -> dict[str, SerializableType]:
    """Describe a receipt for one owned child, never a caller-supplied PID or path."""
    result: dict[str, SerializableType] = {"role": role, "outcome": "unverified"}
    receipt = child._current_receipt() if isinstance(child, TrialAsyncProcess) else None
    if receipt is not None:
        return {
            "role": role,
            "outcome": "match",
            "proof_kind": "current-image-match-at-ack",
            "actual_sha256": encode_public_digest(receipt.digest),
        }
    proc = child.proc
    if isinstance(child, TrialAsyncProcess):
        result["reason"] = (
            "not-live"
            if child.closed or child.returncode is not None
            else "attestation-not-complete"
        )
        return result
    if proc is None or child.closed or proc.returncode is not None:
        result["reason"] = "not-live"
        return result
    try:
        digest = await asyncio.to_thread(_read_child_digest, proc.pid)
    except OSError as err:
        result |= _attestation_os_error(err)
        return result
    except ChildAttestationChangedError:
        result["reason"] = "child-changed-during-attestation"
        return result
    except ValueError:
        result["reason"] = "executable-unstable-or-ineligible"
        return result
    if child.proc is not proc or child.closed or proc.returncode is not None:
        result["reason"] = "child-changed-during-attestation"
        return result
    if digest != EXPECTED_SHA:
        result["outcome"] = "mismatch"
        return result
    result["actual_sha256"] = encode_public_digest(digest)
    result["outcome"] = "match"
    return result


def _attestation_os_error(err: OSError) -> dict[str, SerializableType]:
    """Map procfs failures to finite diagnostics without exposing exception text."""
    if err.errno in (errno.EACCES, errno.EPERM):
        reason = "procfs-access-denied"
    elif err.errno in (errno.ENOENT, errno.ENOTDIR, errno.ESRCH):
        reason = "procfs-unavailable-or-child-exited"
    else:
        reason = "procfs-read-failed"
    result: dict[str, SerializableType] = {"reason": reason}
    if err.errno is not None:
        result["errno"] = err.errno
    return result


def _digest_fd(fd: int) -> tuple[str, bytes]:
    """Hash a bounded regular descriptor from offset zero without changing its offset."""
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_BINARY_SIZE:
        raise ValueError("Trial executable is not a bounded regular file")
    digest = hashlib.sha256()
    total = 0
    header = b""
    while block := os.pread(fd, min(65536, MAX_BINARY_SIZE + 1 - total), total):
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


def _open_validated_asset() -> tuple[int, str]:
    """Open the fixed non-symlink asset and return its retained validated descriptor."""
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
            digest, header = _validate_open_asset(asset_fd)
        except BaseException:
            os.close(asset_fd)
            raise
    except ValueError as err:
        raise TrialBinaryError("Invalid bounded trial asset") from err
    finally:
        os.close(fd)
    try:
        _validate_elf_and_digest(header, digest)
    except BaseException:
        os.close(asset_fd)
        raise
    return asset_fd, digest


def _validate_open_asset(asset_fd: int) -> tuple[str, bytes]:
    """Validate executable mode and immutable bytes of an opened asset descriptor."""
    if not os.fstat(asset_fd).st_mode & 0o111:
        raise TrialBinaryError("Trial asset is not executable")
    return _digest_fd(asset_fd)


def _validate_elf_and_digest(header: bytes, digest: str) -> None:
    """Validate the fixed AArch64 ELF header and pinned digest."""
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


def _validate_asset() -> str:
    """Validate the fixed asset while retaining no mutable path capability."""
    fd, digest = _open_validated_asset()
    os.close(fd)
    return digest


async def create_private_process(
    args: list[str],
    *,
    attest_only: bool = False,
    stdin: bool | int | None = None,
    stdout: bool | int | None = None,
    stderr: bool | int | None = False,
    name: str | None = None,
    env: dict[str, str] | None = None,
    pass_fds: tuple[int, ...] = (),
) -> AsyncProcess:
    """Create the sole private CLI launch path without hashing mutable media on the event loop."""
    if not is_trial_target():
        return AsyncProcess(args, stdin, stdout, stderr, name, env, pass_fds)
    asset_fd = await open_in_thread(
        _open_private_asset_fd, str(TRIAL_ROOT / EXPECTED_SHA / ASSET_NAME), os.O_RDONLY
    )
    try:
        return TrialAsyncProcess(
            args,
            asset_fd=asset_fd,
            attest_only=attest_only,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            name=name,
            env=env,
            pass_fds=pass_fds,
        )
    except BaseException:
        os.close(asset_fd)
        raise


def _open_private_asset_fd(path: str, flags: int) -> int:
    """Adapt fixed-asset validation to the shared cancellation-safe descriptor opener."""
    if path != str(TRIAL_ROOT / EXPECTED_SHA / ASSET_NAME) or flags != os.O_RDONLY:
        raise TrialBinaryError("Unexpected private asset open request")
    asset_fd, _digest = _open_validated_asset()
    return asset_fd


async def preflight_current_image() -> None:
    """Require the target's protected self-read acknowledgement before any network I/O."""
    proc = await create_private_process(
        [str(TRIAL_ROOT / EXPECTED_SHA / ASSET_NAME), "--attest-only"],
        attest_only=True,
        stdout=True,
    )
    try:
        await proc.start()
        returncode = await proc.wait_with_timeout(ATTEST_TIMEOUT)
    except TimeoutError as err:
        raise TrialBinaryError("Trial image preflight timed out") from err
    finally:
        await proc.close()
    if returncode != 0:
        raise TrialBinaryError("Trial image preflight failed")


async def _check_binary(path: Path) -> str:
    """Retain the validated descriptor through the build check, never reopening its pathname."""
    asset_fd = await open_in_thread(_open_private_asset_fd, str(path), os.O_RDONLY)
    try:
        return await _check_open_descriptor(asset_fd)
    finally:
        os.close(asset_fd)


async def _check_open_descriptor(asset_fd: int) -> str:
    """Run the retained executable's check with bounded time and combined output."""
    proc = await asyncio.create_subprocess_exec(
        descriptor_path(asset_fd),
        "--check",
        pass_fds=(asset_fd,),
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
                raise ChildAttestationChangedError("Owned executable changed during attestation")
            return digest
        finally:
            os.close(fd)
    finally:
        os.close(proc_fd)
