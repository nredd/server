"""Offline regression coverage for explicit operator scope validation."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest

from scripts.private_trial_scope import TrialScope, check_players

ROOT = Path(__file__).resolve().parents[2]


def inventory() -> dict[str, object]:
    """Return synthetic, disjoint operator inventory."""
    return {
        "target": "021122334455@test-speaker",
        "target_ip": "192.0.2.10",
        "target_link_mac": "02:11:22:33:44:55",
        "excluded": [{"ip": "198.51.100.20", "mac": "02:aa:bb:cc:dd:ee"}],
        "host_ip": "203.0.113.30",
        "ssh_host": "trial-host",
        "mode": "homepod-ptp",
    }


def test_valid() -> None:
    """Derive the player and both filters from one inventory."""
    scope = TrialScope.parse(inventory())
    assert scope.player == "ap021122334455"
    assert "host 192.0.2.10" in scope.capture_filter
    assert "192.0.2.10" not in scope.excluded_filter
    assert "host 198.51.100.20" in scope.excluded_filter
    assert "ether host 02:aa:bb:cc:dd:ee" in scope.excluded_filter


def test_missing_fields() -> None:
    """Require every field rather than reusing saved wizard state."""
    for field in inventory():
        data = inventory()
        del data[field]
        with pytest.raises(ValueError, match=r".+"):
            TrialScope.parse(data)


def test_invalid_fields() -> None:
    """Reject unsupported protocols, shell syntax and ambiguous endpoints."""
    cases: dict[str, list[object]] = {
        "target": ["", "bad", '021122334455@bad"name', "021122334455@bad;name"],
        "target_ip": ["", "localhost", "192.0.2.10;id", "::1", "224.0.0.1"],
        "target_link_mac": ["", "ff:ff:ff:ff:ff:ff", "00:00:00:00:00:00"],
        "host_ip": ["192.0.2.10", "198.51.100.20", "0.0.0.0"],
        "ssh_host": ["-oProxyCommand=id", "host;id", "user@host", ""],
        "excluded": [[], [{}], [{"ip": "192.0.2.10"}], [{"ip": "::1"}]],
        "mode": ["tv", "raop", "", None],
    }
    for field, values in cases.items():
        for value in values:
            data = inventory() | {field: value}
            with pytest.raises(ValueError, match=r".+"):
                TrialScope.parse(data)


def test_overlapping_macs_and_duplicates() -> None:
    """Reject link-layer overlap and duplicate addresses, including aliases."""
    for excluded in (
        [{"ip": "198.51.100.20", "mac": "02:11:22:33:44:55"}],
        [{"ip": "198.51.100.20"}, {"ip": "198.51.100.20"}],
    ):
        with pytest.raises(ValueError, match=r".+"):
            TrialScope.parse(inventory() | {"excluded": excluded})


def test_optional_excluded_mac() -> None:
    """Accept explicit IP-only exclusions without discovering them."""
    scope = TrialScope.parse(inventory() | {"excluded": [{"ip": "198.51.100.20"}]})
    assert "ether host" not in scope.excluded_filter


def test_unknown_fields() -> None:
    """Reject misspelled configuration rather than ignoring it."""
    with pytest.raises(ValueError, match=r".+"):
        TrialScope.parse(inventory() | {"typo": True})


def test_player_readiness_scope() -> None:
    """Only the exact admitted HomePod may use the PTP readiness lane."""
    player = {
        "player_id": "ap021122334455",
        "device_info": {"manufacturer": "Apple", "model": "HomePod Mini"},
    }
    check_players([player], "ap021122334455")
    for players in ([], [player, player], [player | {"player_id": "apaaaaaaaaaaaa"}]):
        with pytest.raises(ValueError, match=r".+"):
            check_players(players, "ap021122334455")
    tv = player | {"device_info": {"manufacturer": "Apple", "model": "Apple TV"}}
    with pytest.raises(ValueError, match=r".+"):
        check_players([tv], "ap021122334455")


async def test_wizard_rejects_before_remote_io() -> None:
    """Exercise actual shell entry with missing, invalid and overlapping scope."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        marker = root / "remote-called"
        ssh = root / "ssh"
        ssh.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 99\n")
        ssh.chmod(0o700)
        for data in (None, {}, inventory() | {"host_ip": "192.0.2.10"}):
            scope_file = root / "scope.json"
            if data is not None:
                scope_file.write_text(json.dumps(data))
            process = await asyncio.create_subprocess_exec(
                "/bin/bash",
                str(ROOT / "scripts/private_trial_window.sh"),
                str(scope_file),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=os.environ
                | {"PATH": f"{root}:{os.environ['PATH']}", "ENV_FILE": str(root / "window.env")},
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(b"\\n" * 10), 5)
            assert process.returncode != 0
            assert not marker.exists(), stdout + stderr
