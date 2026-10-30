"""Exercise the actual wizard verdict with synthetic offline packet captures."""

from __future__ import annotations

import asyncio
import os
import shutil
import struct
from ipaddress import IPv4Address
from pathlib import Path

import pytest

from scripts.private_trial_scope import TrialScope
from tests.scripts.test_private_trial_scope import ROOT, inventory

TCPDUMP = shutil.which("tcpdump")
pytestmark = pytest.mark.skipif(TCPDUMP is None, reason="offline verdict test requires tcpdump")


def write_pcap(path: Path, destinations: list[str]) -> None:
    """Write Ethernet/IPv4/UDP packets without sending anything on the network."""
    data = struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
    for destination in destinations:
        ethernet = bytes.fromhex("02aabbccddee0200000000010800")
        header = struct.pack(
            "!BBHHHBBH4s4s",
            0x45,
            0,
            28,
            0,
            0,
            64,
            17,
            0,
            IPv4Address("203.0.113.30").packed,
            IPv4Address(destination).packed,
        )
        packet = ethernet + header + struct.pack("!HHHH", 5555, 7000, 8, 0)
        data += struct.pack("<IIII", 10, 0, len(packet), len(packet)) + packet
    path.write_bytes(data)


@pytest.mark.parametrize(
    ("destination", "passed", "kind"),
    [
        ("192.0.2.10", True, "udp"),
        ("198.51.100.20", False, "udp"),
        ("198.51.100.20", False, "non-ip"),
        ("198.51.100.20", False, "icmp-baseline"),
    ],
)
async def test_actual_verdict(tmp_path: Path, destination: str, passed: bool, kind: str) -> None:
    """Target traffic passes; excluded traffic fails even with an empty baseline."""
    capture = tmp_path / "window.pcap"
    # Keep Ethernet destination consistent with the selected synthetic device.
    write_pcap(capture, [destination])
    if passed:
        capture.write_bytes(
            capture.read_bytes().replace(
                bytes.fromhex("02aabbccddee"), bytes.fromhex("021122334455")
            )
        )
    baseline = ""
    if kind == "non-ip":
        packet = bytes.fromhex("02aabbccddee02000000000188b5") + bytes(46)
        capture.write_bytes(
            capture.read_bytes()[:24] + struct.pack("<IIII", 10, 0, 60, 60) + packet
        )
    elif kind == "icmp-baseline":
        icmp_packet = bytearray(capture.read_bytes())
        icmp_packet[63] = 1  # IPv4 protocol, after pcap/record/Ethernet headers.
        icmp_packet[74:82] = struct.pack("!BBHHH", 8, 0, 0, 1, 1)
        capture.write_bytes(icmp_packet)
        baseline = "203.0.113.30 > 198.51.100.20\n"
    (tmp_path / "baseline.flows").write_text(baseline)
    script = (ROOT / "scripts/private_trial_window.sh").read_text()
    flowkey = next(line for line in script.splitlines() if line.startswith("FLOWKEY="))
    verdict = script[script.index("VERDICT=$(remote") :]
    command = "\n".join(
        [
            "set -euo pipefail",
            TrialScope.parse(inventory()).shell(),
            f"CAP_FILE='{capture}'",
            f"BASE='{tmp_path / 'baseline'}'",
            "WINDOW_START_EPOCH=0; ABORTED=0; GREEN=; RED=; RESET=",
            'remote() { /bin/bash -c "$1"; }',
            "say() { :; }; warn() { :; }; finish() { :; }; fail() { exit 1; }",
            flowkey,
            verdict,
        ]
    )
    process = await asyncio.create_subprocess_exec(
        "/bin/bash",
        "-c",
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ | {"PATH": f"{Path(str(TCPDUMP)).parent}:{os.environ['PATH']}"},
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
    assert (process.returncode == 0) is passed, stdout + stderr
    assert (b"ISOLATION PASS" in stdout) is passed
    if kind == "icmp-baseline":
        assert b"HOST_TO_EXCLUDED 1" in stdout
        assert b"NEW_EXCLUDED_FLOWS 0" in stdout
    if kind == "non-ip":
        assert b"NON_IP 0" not in stdout


async def test_unreadable_capture_never_passes(tmp_path: Path) -> None:
    """A failed tcpdump must not become zero counters through a pipeline."""
    scope = TrialScope.parse(inventory())
    process = await asyncio.create_subprocess_exec(
        "/bin/bash",
        "-c",
        f"set -euo pipefail; tcpdump -r '{tmp_path / 'missing'}' '{scope.excluded_filter}' | wc -l",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ | {"PATH": f"{Path(str(TCPDUMP)).parent}:{os.environ['PATH']}"},
    )
    await asyncio.wait_for(process.communicate(), 10)
    assert process.returncode != 0
