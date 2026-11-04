"""
Run the receiver-free private CLI artifact gate before any trial pin adoption.

This tool invokes the AirPlay provider's actual bounded asset check and current-image
preflight.  It starts no server, provider, receiver connection, or playback session.

References:
- `PRIVATE_NATIVE_AIRPLAY_TRIAL.md`
"""

from __future__ import annotations

import asyncio
import sys

from music_assistant.providers.airplay import trial_binary


def main() -> int:
    """Run the provider's exact asset validation and FD-bound ACK preflight."""
    try:
        asyncio.run(_run_gate())
    except trial_binary.TrialBinaryError as err:
        sys.stderr.write(f"Private CLI artifact gate rejected: {err}\n")
        return 1
    sys.stdout.write(
        "Private CLI artifact gate passed: checked asset and current-image preflight\n"
    )
    return 0


async def _run_gate() -> None:
    """Use the same ordering as private AirPlay provider initialization."""
    await trial_binary.validate_trial_binary()
    await trial_binary.preflight_current_image()


if __name__ == "__main__":
    raise SystemExit(main())
