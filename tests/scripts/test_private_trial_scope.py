"""Offline regression coverage for explicit operator scope validation."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest

from scripts.private_trial_scope import CaptureVisibility, TrialMode, TrialScope, check_players

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


def tv_inventory() -> dict[str, object]:
    """Return a synthetic TV RAOP inventory with the same exclusion contract."""
    return inventory() | {"target": "021122334455@test-tv", "mode": "tv-raop"}


def test_valid() -> None:
    """Derive the player and both filters from one inventory."""
    scope = TrialScope.parse(inventory())
    assert scope.player == "ap021122334455"
    assert "host 192.0.2.10" in scope.capture_filter
    assert "192.0.2.10" not in scope.excluded_filter
    assert "host 198.51.100.20" in scope.excluded_filter
    assert "ether host 02:aa:bb:cc:dd:ee" in scope.excluded_filter
    assert scope.mode == "homepod-ptp"


def test_tv_scope_preserves_explicit_exclusions() -> None:
    """Admit the TV lane without changing the target or exclusion filter contract."""
    scope = TrialScope.parse(tv_inventory())
    assert scope.mode == "tv-raop"
    assert scope.target == "021122334455@test-tv"
    assert scope.player == "ap021122334455"
    assert "host 192.0.2.10" in scope.capture_filter
    assert "host 198.51.100.20" in scope.excluded_filter
    assert "ether host 02:aa:bb:cc:dd:ee" in scope.excluded_filter


def routed_inventory() -> dict[str, object]:
    """Return a synthetic routed inventory: explicit visibility, no link MACs."""
    data = inventory() | {
        "capture_visibility": "routed-ip",
        "excluded": [{"ip": "198.51.100.20"}],
    }
    del data["target_link_mac"]
    return data


def test_default_visibility_is_on_link() -> None:
    """Existing inventories keep the on-link contract and filters unchanged."""
    scope = TrialScope.parse(inventory())
    assert scope.visibility == CaptureVisibility.ON_LINK
    assert "ether host 02:11:22:33:44:55" in scope.capture_filter
    explicit = TrialScope.parse(inventory() | {"capture_visibility": "on-link"})
    assert explicit.capture_filter == scope.capture_filter
    assert "CAPTURE_VISIBILITY=on-link" in scope.shell()


def test_routed_scope_is_ip_only_and_host_paired() -> None:
    """Routed filters match only HAOS<->declared IPv4 endpoints, never MACs."""
    scope = TrialScope.parse(routed_inventory())
    assert scope.visibility == CaptureVisibility.ROUTED_IP
    assert (
        scope.capture_filter
        == "ip and host 203.0.113.30 and (host 198.51.100.20 or host 192.0.2.10)"
    )
    assert scope.excluded_filter == "ip and host 203.0.113.30 and (host 198.51.100.20)"
    assert "ether" not in scope.capture_filter + scope.excluded_filter
    assert "multicast" not in scope.capture_filter
    assert "CAPTURE_VISIBILITY=routed-ip" in scope.shell()


def test_routed_rejects_mac_fields_instead_of_dropping_them() -> None:
    """A MAC supplied under routed-ip (e.g. a gateway MAC) is refused, not ignored."""
    with pytest.raises(ValueError, match="forbids `target_link_mac`"):
        TrialScope.parse(routed_inventory() | {"target_link_mac": "02:00:00:00:00:01"})
    with pytest.raises(ValueError, match="forbids excluded link `mac`"):
        TrialScope.parse(
            routed_inventory() | {"excluded": [{"ip": "198.51.100.20", "mac": "02:aa:bb:cc:dd:ee"}]}
        )


def test_routed_still_requires_other_fields_and_known_visibility() -> None:
    """Routed drops only the MAC requirement; the rest stays exact."""
    for field in ("target", "target_ip", "excluded", "host_ip", "ssh_host", "mode"):
        data = routed_inventory()
        del data[field]
        with pytest.raises(ValueError, match="documented inventory fields"):
            TrialScope.parse(data)
    for value in ("routed", "", "ROUTED-IP"):
        with pytest.raises(ValueError, match="Invalid `capture_visibility`"):
            TrialScope.parse(routed_inventory() | {"capture_visibility": value})
    with pytest.raises(ValueError, match="Invalid `capture_visibility`"):
        TrialScope.parse(routed_inventory() | {"capture_visibility": None})
    with pytest.raises(ValueError, match="documented inventory fields"):
        TrialScope.parse(routed_inventory() | {"gateway_mac": "02:00:00:00:00:01"})


def test_on_link_without_mac_still_refuses() -> None:
    """Omitting target_link_mac never silently selects routed mode."""
    data = inventory()
    del data["target_link_mac"]
    with pytest.raises(ValueError, match=r".+"):
        TrialScope.parse(data)


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
        "mode": ["tv", "raop", "homepod", "", None],
    }
    for field, values in cases.items():
        for value in values:
            data = inventory() | {field: value}
            with pytest.raises((TypeError, ValueError), match=r".+"):
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


def test_tv_window_skips_the_homepod_ptp_and_playback_lane() -> None:
    """Keep TV scope admission-only instead of reusing the HomePod trial sequence."""
    script = (ROOT / "scripts/private_trial_window.sh").read_text()
    stage = script[script.index('stage "Verify the mode-specific admission boundary"') :]
    tv_start = stage.index('if [[ "$TRIAL_MODE" == "tv-raop" ]]')
    homepod_start = stage.index("  else\n    READY", tv_start)
    tv_branch = stage[tv_start:homepod_start]
    homepod_branch = stage[homepod_start : stage.index("# ── 11")]
    assert "private_native_volume_capture/readiness/get" not in tv_branch
    assert "players/cmd/volume_set" not in tv_branch
    assert "player_queues/play_media" not in tv_branch
    assert "private_native_volume_capture/readiness/get" in homepod_branch
    assert "players/cmd/volume_set" in homepod_branch
    assert "player_queues/play_media" in homepod_branch


def test_player_scope_rechecks_receiver_class_by_mode() -> None:
    """Require exactly the target class for the selected mode."""
    homepod = {
        "player_id": "ap021122334455",
        "device_info": {"manufacturer": "Apple", "model": "HomePod Mini"},
    }
    tv = homepod | {"device_info": {"manufacturer": "Apple", "model": "Apple TV 4K Gen3"}}
    check_players([homepod], "ap021122334455", TrialMode.HOME_POD_PTP)
    check_players([tv], "ap021122334455", TrialMode.TV_RAOP)
    for players, mode in (
        ([], TrialMode.HOME_POD_PTP),
        ([homepod, homepod], TrialMode.HOME_POD_PTP),
        ([homepod | {"player_id": "apaaaaaaaaaaaa"}], TrialMode.HOME_POD_PTP),
        ([tv], TrialMode.HOME_POD_PTP),
        ([homepod], TrialMode.TV_RAOP),
    ):
        with pytest.raises(ValueError, match=r".+"):
            check_players(players, "ap021122334455", mode)


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
