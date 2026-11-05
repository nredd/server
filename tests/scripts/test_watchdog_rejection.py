"""Offline checks for the watchdog's fail-closed readiness contract."""

from __future__ import annotations

import os
import time
from typing import Any

import pytest

from scripts import private_tv_transport as transport


@pytest.mark.parametrize("response", [b"0", b"", None])
def test_watchdog_rejects_invalid_eof_or_missing_ack(
    response: bytes | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never accept an invalid byte, EOF, or a silent child as readiness."""
    held: list[int] = []

    def offline_spawn(
        _executable: str, _argv: list[str], _env: dict[str, str], **kwargs: Any
    ) -> int:
        """Supply an offline child response without creating any process or client."""
        write_fd = kwargs["file_actions"][1][1]
        if response is None:
            held.append(os.dup(write_fd))
        else:
            os.write(write_fd, response)
        return 0

    monkeypatch.setattr(os, "posix_spawn", offline_spawn)
    try:
        with pytest.raises(transport.TransportError, match="stop watchdog did not become ready"):
            transport.StopWatchdog("http://offline.invalid", "synthetic", "OFFLINE_TOKEN").arm(
                "synthetic-player", time.monotonic() + transport.HTTP_TIMEOUT_SECONDS + 0.1
            )
    finally:
        for fd in held:
            os.close(fd)


def test_watchdog_rejects_expired_reserve_before_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse an unusable deadline before allocating the readiness pipe."""

    def unexpected_pipe() -> tuple[int, int]:
        """Fail if a rejected deadline allocates resources."""
        pytest.fail("expired deadline allocated a pipe")

    monkeypatch.setattr(os, "pipe", unexpected_pipe)
    with pytest.raises(transport.TransportError, match="reserve its HTTP budget"):
        transport.StopWatchdog("http://offline.invalid", "synthetic", "OFFLINE_TOKEN").arm(
            "synthetic-player", time.monotonic() + transport.HTTP_TIMEOUT_SECONDS
        )
