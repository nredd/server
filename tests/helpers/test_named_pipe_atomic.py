"""Atomic transaction and cancellation guarantees for checked CLI volume IPC."""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from music_assistant.helpers.named_pipe import AsyncNamedPipeWriter


async def test_atomic_transaction_is_one_write_and_rejects_oversize(tmp_path: Path) -> None:
    """Never split a transaction into writes smaller than the platform's PIPE_BUF."""
    writer = AsyncNamedPipeWriter(str(tmp_path / "commands"))
    await writer.create()
    reader = os.open(writer.path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        assert await writer.wait_for_reader(1)
        limit = os.fpathconf(reader, "PC_PIPE_BUF")
        with patch("music_assistant.helpers.named_pipe.os.write", wraps=os.write) as write:
            assert await writer.write_atomic(b"field=1\nACTION=test\n")
            assert write.call_count == 1
            assert not await writer.write_atomic(b"x" * (limit + 1))
            assert write.call_count == 1
        assert os.read(reader, limit) == b"field=1\nACTION=test\n"
    finally:
        os.close(reader)
        await writer.remove()


async def test_cancelled_atomic_write_cannot_send_after_pipe_drains(tmp_path: Path) -> None:
    """A cancelled full-pipe transaction leaves no worker retrying behind the lock."""
    writer = AsyncNamedPipeWriter(str(tmp_path / "commands"))
    await writer.create()
    reader = os.open(writer.path, os.O_RDONLY | os.O_NONBLOCK)
    reached = threading.Event()
    wait_writable = writer._wait_writable

    def wait(fd: int) -> None:
        reached.set()
        wait_writable(fd)

    try:
        assert await writer.wait_for_reader(1)
        assert writer._write_fd is not None
        while True:
            try:
                os.write(writer._write_fd, b"x" * 512)
            except BlockingIOError:
                break
        with patch.object(writer, "_wait_writable", side_effect=wait):
            task = asyncio.create_task(writer.write_atomic(b"NEVER_SEND=1\nACTION=test\n"))
            assert await asyncio.to_thread(reached.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        contents = bytearray()
        while True:
            try:
                contents.extend(os.read(reader, 65536))
            except BlockingIOError:
                break
        assert b"NEVER_SEND" not in contents
        assert await writer.write_atomic(b"NEW=1\nACTION=test\n")
        assert os.read(reader, 512) == b"NEW=1\nACTION=test\n"
    finally:
        os.close(reader)
        await writer.remove()
