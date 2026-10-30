"""
Validate the operator's offline trial inventory before any remote action.

References: https://www.rfc-editor.org/rfc/rfc5737
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from dataclasses import dataclass
from ipaddress import IPv4Address
from pathlib import Path

TARGET = re.compile(r"([0-9a-fA-F]{12})@([A-Za-z0-9][A-Za-z0-9_.-]{0,62})")
MAC = re.compile(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}")
SSH_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,252}")
FIELDS = {"target", "target_ip", "target_link_mac", "excluded", "host_ip", "ssh_host", "mode"}


@dataclass(frozen=True)
class TrialScope:
    """One validated scope shared by admission, capture and verdict."""

    target: str
    player: str
    target_ip: str
    host_ip: str
    ssh_host: str
    capture_filter: str
    excluded_filter: str

    @classmethod
    def parse(cls, data: object) -> TrialScope:
        """Validate a complete explicit inventory, rejecting ambiguous scope."""
        if not isinstance(data, dict) or set(data) != FIELDS:
            raise ValueError("Scope requires exactly the documented inventory fields")
        target = data["target"]
        if not isinstance(target, str) or not (match := TARGET.fullmatch(target)):
            raise ValueError("Target must be a MAC@name with a safe DNS instance name")
        if data["mode"] != "homepod-ptp":
            raise ValueError("Only homepod-ptp is supported; TV/RAOP readiness is unsupported")
        ssh_host = data["ssh_host"]
        if not isinstance(ssh_host, str) or not SSH_HOST.fullmatch(ssh_host):
            raise ValueError("ssh_host must be a plain SSH host alias")
        target_ip = _ipv4(data["target_ip"])
        host_ip = _ipv4(data["host_ip"])
        target_mac = _mac(data["target_link_mac"])
        if target_ip == host_ip:
            raise ValueError("Target and host addresses overlap")
        excluded = data["excluded"]
        if not isinstance(excluded, list) or not excluded:
            raise ValueError("Excluded inventory must be explicit and nonempty")
        seen = {target_ip, host_ip}
        terms: list[str] = []
        for entry in excluded:
            if not isinstance(entry, dict) or set(entry) not in ({"ip"}, {"ip", "mac"}):
                raise ValueError("Each excluded endpoint requires ip and optional mac")
            address = _ipv4(entry["ip"])
            if address in seen:
                raise ValueError("Duplicate or overlapping inventory addresses")
            seen.add(address)
            terms.append(f"host {address}")
            if "mac" in entry:
                mac = _mac(entry["mac"])
                if mac in (
                    target_mac,
                    ":".join(match[1][i : i + 2] for i in range(0, 12, 2)).lower(),
                ):
                    raise ValueError("Target and excluded link-layer identities overlap")
                terms.append(f"ether host {mac}")
        excluded_filter = f"({' or '.join(terms)}) and not multicast and not broadcast"
        capture_filter = (
            f"({' or '.join([*terms, f'host {target_ip}', f'ether host {target_mac}'])})"
            " and not multicast and not broadcast"
        )
        return cls(
            f"{match[1].lower()}@{match[2]}",
            f"ap{match[1].lower()}",
            target_ip,
            host_ip,
            ssh_host,
            capture_filter,
            excluded_filter,
        )

    def shell(self) -> str:
        """Emit quoted assignments only after the whole scope has validated."""
        values = {
            "TRIAL_TARGET": self.target,
            "TRIAL_PLAYER": self.player,
            "TARGET_IP": self.target_ip,
            "HAOS_IP": self.host_ip,
            "HOST": self.ssh_host,
            "CAP_FILTER": self.capture_filter,
            "EXCLUDED_FILTER": self.excluded_filter,
        }
        return "\n".join(f"{key}={shlex.quote(value)}" for key, value in values.items())


def check_players(data: object, player_id: str) -> None:
    """Refuse readiness unless exactly the admitted HomePod is registered."""
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise ValueError("Malformed players response")
    players = [item for item in data if str(item.get("player_id", "")).startswith("ap")]
    if len(players) != 1 or players[0].get("player_id") != player_id:
        raise ValueError("Registered AirPlay players differ from the validated scope")
    info = players[0].get("device_info")
    if (
        not isinstance(info, dict)
        or info.get("manufacturer") != "Apple"
        or info.get("model") not in ("HomePod", "HomePod Mini", "HomePod 2")
    ):
        raise ValueError(
            "Unsupported target class: this wizard only verifies HomePod PTP readiness"
        )


def main() -> int:
    """Validate an inventory file or a readiness player response, without network I/O."""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--scope", type=Path)
    mode.add_argument("--check-players", metavar="PLAYER_ID")
    args = parser.parse_args()
    try:
        if args.scope is not None:
            sys.stdout.write(TrialScope.parse(json.loads(args.scope.read_text())).shell() + "\n")
        else:
            check_players(json.load(sys.stdin), args.check_players)
    except (ValueError, TypeError, OSError) as err:
        sys.stderr.write(f"Scope refused: {err}\n")
        return 2
    return 0


def _ipv4(value: object) -> str:
    """Require a canonical unicast IPv4 inventory address, never a hostname."""
    if not isinstance(value, str):
        raise TypeError("Inventory addresses must be IPv4 strings")
    address = IPv4Address(value)
    if address.is_multicast or address.is_unspecified or int(address) == 0xFFFFFFFF:
        raise ValueError("Inventory requires unicast IPv4 addresses")
    return str(address)


def _mac(value: object) -> str:
    """Require an explicit unicast link-layer identity."""
    if not isinstance(value, str) or not MAC.fullmatch(value):
        raise ValueError("Invalid link-layer address")
    value = value.lower()
    if value == "00:00:00:00:00:00" or int(value[:2], 16) & 1:
        raise ValueError("Link-layer address must be nonzero unicast")
    return value


if __name__ == "__main__":
    sys.exit(main())
