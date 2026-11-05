"""
Offline regression for watchdog readiness on high-numbered owned pipe FDs.

References:
- https://docs.python.org/3/library/os.html#os.posix_spawn
- https://docs.python.org/3/library/fcntl.html
"""

from __future__ import annotations

import fcntl
import os
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

from scripts import private_tv_transport as transport


@pytest.mark.parametrize("minimum_fd", [64, 1024])
def test_real_child_acknowledges_and_stops_at_deadline(
    minimum_fd: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise real child readiness and natural exit without an HTTP client."""
    original_pipe = os.pipe
    original_spawn = os.posix_spawn
    children: list[int] = []
    owned: list[int] = []
    result = tmp_path / "stop.txt"
    child_code = textwrap.dedent(
        """
        import sys
        import time
        from pathlib import Path
        from scripts import private_tv_transport as transport

        result = Path(sys.argv.pop(1))
        transport.MusicAssistantApi = lambda *_: type(
            "OfflineApi", (), {"command": lambda self, *args: result.write_text(str(time.monotonic()))}
        )()
        sys.exit(transport.main(sys.argv[1:]))
        """
    )

    def high_pipe() -> tuple[int, int]:
        """Duplicate only two owned descriptors, leaving resource limits unchanged."""
        read_fd, write_fd = original_pipe()
        try:
            high_read = fcntl.fcntl(read_fd, fcntl.F_DUPFD_CLOEXEC, minimum_fd)
            owned.append(high_read)
            high_write = fcntl.fcntl(write_fd, fcntl.F_DUPFD_CLOEXEC, minimum_fd)
            owned.append(high_write)
            return high_read, high_write
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def offline_spawn(executable: str, argv: list[str], env: dict[str, str], **kwargs: Any) -> int:
        """Keep real spawn actions but substitute a child with an offline API."""
        pid = original_spawn(
            executable, [sys.executable, "-c", child_code, str(result), *argv[2:]], env, **kwargs
        )
        children.append(pid)
        return pid

    monkeypatch.setattr(os, "pipe", high_pipe)
    monkeypatch.setattr(os, "posix_spawn", offline_spawn)
    start = time.monotonic()
    deadline = start + transport.HTTP_TIMEOUT_SECONDS + 0.8
    try:
        transport.StopWatchdog("http://offline.invalid", "synthetic", "OFFLINE_TOKEN").arm(
            "synthetic-player", deadline
        )
        assert not result.exists(), "arming must acknowledge before stop dispatch"
    finally:
        # Natural exit, no cancellation that could conceal a missed stop deadline.
        for pid in children:
            _, status = os.waitpid(pid, 0)
            assert os.waitstatus_to_exitcode(status) == 0
        for fd in owned:
            with pytest.raises(OSError, match="Bad file descriptor"):
                os.fstat(fd)
    stopped_at = float(result.read_text())
    assert deadline - transport.HTTP_TIMEOUT_SECONDS <= stopped_at <= deadline
    assert time.monotonic() - start < transport.HTTP_TIMEOUT_SECONDS + 1.5
