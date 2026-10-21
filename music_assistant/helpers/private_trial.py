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
_SHA = re.compile(r"[0-9a-f]{40}")
_ID = re.compile(r"[0-9a-f]{32}")
_FRESH_ROOTS: set[Path] = set()


class PrivateTrialError(RuntimeError):
    """Fail closed without exposing private data or inspecting original configuration."""


def is_trial_target() -> bool:
    """Identify only this private source overlay's fixed Linux ARM profile."""
    return platform.system().lower() == "linux" and platform.machine().lower() in (
        "aarch64",
        "arm64",
    )


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
        return {"status": "verified-installer-provenance", "commit": commit}
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
