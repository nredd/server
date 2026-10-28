"""
Private Linux ARM trial bootstrap, NEVER upstream.

References:
    https://packaging.python.org/en/latest/specifications/direct-url-data-structure/
"""

from __future__ import annotations

import importlib.metadata
import json
import platform
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import music_assistant
from music_assistant.helpers.json import json_loads

if TYPE_CHECKING:
    from music_assistant.helpers.json import SerializableType

APP_DATA = Path("/data")
NAMESPACE = ".native-radio-trial-7d964670ba5d42fd90a3a709c71ec462"
MARKER = "trial-marker.json"
VERSION = 1
MAX_JSON = 1024 * 1024
PRIVATE_API_PORT = 18095
TRIAL_DEFAULT_PROVIDER_DOMAINS = frozenset({"airplay"})
TRIAL_DISABLED_BUILTIN_PROVIDER_DOMAINS = frozenset({"sendspin", "sendspin_source"})
# Private discovery core config entry naming the ONLY device this profile may query,
# as "<12 lowercase hex MAC>@<advertised name>". Unset means nothing is admitted.
CONF_PRIVATE_TRIAL_TARGET = "private_trial_target"
# Environment the private profile hands every owned cliairplay child so unregistered
# incoming PTP is dropped before any reply. The pinned asset must implement the flag
# for it to have effect; requesting it is a profile fact, not enforcement proof.
ENV_PTP_STRICT_PEERS = "CLIAIRPLAY_PTP_STRICT_PEERS"
_SHA = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_ID = re.compile(r"[0-9a-f]{32}")
_MAC = re.compile(r"[0-9a-f]{12}")
# RAOP record names prefix the device name with its MAC, e.g. "aabbccddeeff@Kelder".
_RAOP_MAC_PREFIX = re.compile(r"^(?P<mac>[0-9a-f]{12})@")
RAOP_SERVICE_TYPE = "_raop._tcp.local."
_FRESH_ROOTS: set[Path] = set()


@dataclass(frozen=True)
class TrialTarget:
    """The single admitted device of the private profile."""

    mac: str
    name: str

    @property
    def player_id(self) -> str:
        """Return the MA AirPlay player id this device registers under."""
        return f"ap{self.mac}"


def parse_trial_target(value: object) -> TrialTarget | None:
    """
    Parse the configured target, or None when nothing may be admitted.

    :param value: The raw `private_trial_target` config value, `<12 hex MAC>@<name>`.
    """
    if not isinstance(value, str) or "@" not in value:
        return None
    mac, name = value.split("@", 1)
    if not _MAC.fullmatch(mac) or not name or name != name.strip():
        return None
    return TrialTarget(mac=mac, name=name)


def admits_mdns_name(target: TrialTarget | None, mdns_name: str, service_type: str) -> bool:
    """
    Return whether a directed query for this mDNS record may leave the host.

    :param target: The admitted device, or None to admit nothing.
    :param mdns_name: The full record name, e.g. `aabbccddeeff@Main._raop._tcp.local.`.
    :param service_type: The service type the record is resolved as.
    """
    if target is None:
        return False
    name, suffix = mdns_name.lower(), f".{service_type.lower()}"
    if not name.endswith(suffix) or len(name) == len(suffix):
        return False
    # Dots are legal inside instance labels, so "Main.Office" must not read as "Main".
    instance = name[: -len(suffix)]
    if match := _RAOP_MAC_PREFIX.match(instance):
        return match.group("mac") == target.mac and instance[match.end() :] == target.name.lower()
    if service_type.lower() == RAOP_SERVICE_TYPE:
        return False
    return instance == target.name.lower()


def admits_player_id(target: TrialTarget | None, player_id: str) -> bool:
    """
    Return whether a resolved device may be set up, probed or registered.

    :param target: The admitted device, or None to admit nothing.
    :param player_id: The MA player id derived from the resolved record, `ap<mac>`.
    """
    return target is not None and player_id == target.player_id


def private_child_env() -> dict[str, str]:
    """Return the environment every owned private CLI child must receive."""
    return {ENV_PTP_STRICT_PEERS: "1"}


class PrivateTrialError(RuntimeError):
    """Fail closed without exposing private data or inspecting original configuration."""


def is_trial_target() -> bool:
    """Identify only this private source overlay's fixed Linux ARM profile."""
    return platform.system().lower() == "linux" and platform.machine().lower() in (
        "aarch64",
        "arm64",
    )


def get_webserver_bind_port(configured_port: int) -> int:
    """Return the private API port for the trial, otherwise the configured port."""
    if is_trial_target():
        return PRIVATE_API_PORT
    return configured_port


def get_default_providers(
    providers: set[tuple[str, bool]],
) -> set[tuple[str, bool]]:
    """Return trial-safe default providers without changing ordinary profiles."""
    if not is_trial_target():
        return providers
    return {
        (domain, require_mdns)
        for domain, require_mdns in providers
        if domain in TRIAL_DEFAULT_PROVIDER_DOMAINS
    }


def should_load_builtin_provider(domain: str) -> bool:
    """Return whether a builtin provider may run for this profile."""
    return not is_trial_target() or domain not in TRIAL_DISABLED_BUILTIN_PROVIDER_DOMAINS


def should_load_provider(domain: str, builtin: bool) -> bool:
    """Return whether a provider may load in this private profile."""
    if not is_trial_target():
        return True
    if builtin:
        return should_load_builtin_provider(domain)
    return domain in TRIAL_DEFAULT_PROVIDER_DOMAINS


def should_setup_ingress() -> bool:
    """Return whether the HA ingress listener may be created."""
    return not is_trial_target()


def prepare_data(data_dir: str, cache_dir: str) -> tuple[str, str]:
    """Select a new private namespace before logger, options or configuration bootstrap."""
    if not is_trial_target():
        return data_dir, cache_dir
    if data_dir != str(APP_DATA) or cache_dir != str(APP_DATA / ".cache"):
        raise PrivateTrialError("Trial requires the fixed official app data contract")
    _safe_directory(APP_DATA)
    root = APP_DATA / NAMESPACE
    try:
        root.mkdir(mode=0o700)
    except FileExistsError:
        _safe_directory(root)
        if {path.name for path in root.iterdir()} != {MARKER, "data"}:
            raise PrivateTrialError("Trial namespace contains unexpected content")
        marker = _read_json(root / MARKER)
        if (
            set(marker) != {"version", "server_id"}
            or type(marker["version"]) is not int
            or marker["version"] != VERSION
        ):
            raise PrivateTrialError("Trial marker is invalid")
        server_id = marker["server_id"]
        if not isinstance(server_id, str) or not _ID.fullmatch(server_id):
            raise PrivateTrialError("Trial marker is incomplete")
        _reject_symlinks(root)
        if _read_json(root / "data" / "settings.json").get("server_id") != server_id:
            raise PrivateTrialError("Trial configuration identity does not match marker")
        _safe_directory(root / "data")
        _safe_directory(root / "data" / ".cache")
        _FRESH_ROOTS.discard(root)
    else:
        # mkdir without exist_ok is the first-boot emptiness proof. Existing empty or
        # seeded namespaces are never adopted, repaired or erased.
        if any(root.iterdir()):
            raise PrivateTrialError("Trial namespace was not empty")
        (root / "data").mkdir(mode=0o700)
        (root / "data" / ".cache").mkdir(mode=0o700)
        with (root / MARKER).open("x", encoding="utf-8") as stream:
            json.dump({"version": VERSION, "server_id": None}, stream)
        _FRESH_ROOTS.add(root)
    return str(root / "data"), str(root / "data" / ".cache")


def validate_bootstrap(storage_path: str, cache_path: str) -> None:
    """Reject embedded starts outside the prepared private namespace before config reads."""
    if not is_trial_target():
        return
    root = APP_DATA / NAMESPACE
    if storage_path != str(root / "data") or cache_path != str(root / "data" / ".cache"):
        raise PrivateTrialError("Trial data was not prepared")
    _safe_directory(root)
    _safe_directory(root / "data")
    _safe_directory(root / "data" / ".cache")
    if {path.name for path in root.iterdir()} != {MARKER, "data"}:
        raise PrivateTrialError("Trial namespace contains unexpected content")
    _reject_symlinks(root)
    marker = _read_json(root / MARKER)
    if (
        set(marker) != {"version", "server_id"}
        or type(marker["version"]) is not int
        or marker["version"] != VERSION
    ):
        raise PrivateTrialError("Trial bootstrap marker is invalid")
    server_id = marker["server_id"]
    if root in _FRESH_ROOTS:
        if marker != {"version": VERSION, "server_id": None}:
            raise PrivateTrialError("Trial first boot marker is invalid")
        allowed = {".cache", "musicassistant.log", "musicassistant.log.1"}
        if any(path.name not in allowed for path in (root / "data").iterdir()):
            raise PrivateTrialError("Trial first boot contains unexpected data")
        if any((root / "data" / ".cache").iterdir()):
            raise PrivateTrialError("Trial first boot contains unexpected cache")
    elif (
        not isinstance(server_id, str)
        or not _ID.fullmatch(server_id)
        or _read_json(root / "data" / "settings.json").get("server_id") != server_id
    ):
        raise PrivateTrialError("Trial restart identity is invalid")


def bind_server_id(storage_path: str, server_id: str) -> None:
    """Bind MA's existing fresh UUID to the marker before any discovery starts."""
    if not is_trial_target():
        return
    root = APP_DATA / NAMESPACE
    if storage_path != str(root / "data") or not _ID.fullmatch(server_id):
        raise PrivateTrialError("Trial bootstrap identity is invalid")
    marker = _read_json(root / MARKER)
    expected = {"version": VERSION, "server_id": None if root in _FRESH_ROOTS else server_id}
    if marker != expected:
        raise PrivateTrialError("Trial bootstrap marker does not match")
    if root in _FRESH_ROOTS:
        with (root / MARKER).open("w", encoding="utf-8") as stream:
            json.dump({"version": VERSION, "server_id": server_id}, stream)
        _FRESH_ROOTS.discard(root)


def encode_public_digest(digest: str) -> dict[str, SerializableType]:
    """Encode a validated public SHA1/SHA256 digest without sanitizer-sensitive text."""
    if not (_SHA.fullmatch(digest) or _SHA256.fullmatch(digest)):
        raise ValueError("Digest is not a lowercase SHA1 or SHA256 value")
    return {
        "encoding": "u32be-v1",
        "words": [int(digest[index : index + 8], 16) for index in range(0, len(digest), 8)],
    }


def installer_provenance() -> dict[str, SerializableType]:
    """Report bounded PEP 610 installer provenance, NOT immutable loaded-byte proof."""
    unverified: dict[str, SerializableType] = {"status": "unverified"}
    try:
        dist = importlib.metadata.distribution("music-assistant")
        root = Path(str(dist.locate_file(""))).resolve()
        origin = Path(str(music_assistant.__file__))
        if origin.is_symlink() or origin.resolve() != root / "music_assistant" / "__init__.py":
            return unverified
        entries = [
            entry
            for entry in dist.files or ()
            if len(entry.parts) == 2
            and entry.parts[0].endswith(".dist-info")
            and entry.parts[1] == "direct_url.json"
        ]
        if len(entries) != 1:
            return unverified
        metadata = Path(str(dist.locate_file(entries[0])))
        if metadata.parent.is_symlink() or metadata.parent.resolve().parent != root:
            return unverified
        data = _read_json(metadata)
        vcs = data.get("vcs_info")
        if (
            data.get("url")
            not in ("https://github.com/nredd/server", "https://github.com/nredd/server.git")
            or "dir_info" in data
            or not isinstance(vcs, dict)
            or vcs.get("vcs") != "git"
        ):
            return unverified
        commit = vcs.get("commit_id")
        if not isinstance(commit, str) or not _SHA.fullmatch(commit):
            return unverified
        return {
            "status": "verified-installer-provenance",
            "commit": encode_public_digest(commit),
        }
    except (
        OSError,
        ValueError,
        TypeError,
        PrivateTrialError,
        importlib.metadata.PackageNotFoundError,
    ):
        return unverified


def _safe_directory(path: Path) -> None:
    """Reject symlinks or non-directories without reading their contents."""
    if path.is_symlink() or not path.is_dir():
        raise PrivateTrialError("Trial directory is unsafe")


def _reject_symlinks(root: Path) -> None:
    """Reject redirected private state on restart without following directory links."""
    for path in root.rglob("*"):
        if path.is_symlink():
            raise PrivateTrialError("Trial state contains a symlink")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject ambiguous duplicate metadata keys rather than selecting a value."""
    result = dict(pairs)
    if len(result) != len(pairs):
        raise PrivateTrialError("Trial metadata contains duplicate keys")
    return result


def _read_json(path: Path) -> dict[str, object]:
    """Read only bounded private marker/settings or own distribution metadata."""
    if path.is_symlink() or not path.is_file():
        raise PrivateTrialError("Trial metadata is unavailable")
    with path.open("rb") as stream:
        raw = stream.read(MAX_JSON + 1)
    if len(raw) > MAX_JSON:
        raise PrivateTrialError("Trial metadata exceeds limit")
    try:
        # Match the real config loader before its fallback can read a backup.
        # stdlib JSON alone accepts NaN and escaped lone surrogates that MA rejects.
        json_loads(raw)
        data = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError) as err:
        raise PrivateTrialError("Trial metadata is invalid") from err
    if not isinstance(data, dict):
        raise PrivateTrialError("Trial metadata is invalid")
    return data
