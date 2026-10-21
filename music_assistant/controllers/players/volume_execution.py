"""
Checked receipts for the existing player-volume execution path.

References:
    - music_assistant/controllers/players/controller.py
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class CheckedVolumeExecution:
    """Internal, fail-closed execution hooks; levels are confirmed logical percentages."""

    validate: Callable[[], None]
    preflight: Callable[[str], None]
    apply: Callable[[str, int], Awaitable[int]]
    levels: dict[str, int]
    reference: dict[str, int] | None = None
