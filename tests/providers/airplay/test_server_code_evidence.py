"""Tests for bounded private server code evidence."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from music_assistant.helpers.private_trial import encode_public_digest
from music_assistant.providers.airplay import (
    constants,
    control_player,
    player,
    provider,
    trial_binary,
)
from music_assistant.providers.airplay import (
    server_code_evidence as evidence,
)


def test_real_sources_and_raw_hash() -> None:
    """Evidence hashes the exact bytes of each selected source."""
    result = evidence.server_code_evidence(provider)
    assert result["status"] == "verified"
    hashes = result["source_hashes"]
    assert isinstance(hashes, dict)
    assert hashes["provider"] == encode_public_digest(
        hashlib.sha256(Path(provider.__file__).read_bytes()).hexdigest()
    )


def test_wrong_provider_module() -> None:
    """A wrong module argument cannot produce verified evidence."""
    assert evidence.server_code_evidence(control_player) == {
        "schema_version": 1,
        "status": "unverified",
    }


def test_stale_code_on_changed_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Changed disk code does not match a previously loaded function."""
    source = Path(provider.__file__).read_bytes()
    changed = source.replace(
        b'default_volume_bounds": default_bounds', b'default_volume_bounds": True'
    )
    assert changed != source
    path = tmp_path / "provider.py"
    path.write_bytes(changed)
    monkeypatch.setattr(provider, "__file__", str(path))
    assert evidence.server_code_evidence(provider) == {"schema_version": 1, "status": "unverified"}


def test_nonsemantic_raw_change_changes_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A comment changes the raw hash even when selected live code is unchanged."""
    before = evidence.server_code_evidence(provider)
    path = tmp_path / "provider.py"
    path.write_bytes(Path(provider.__file__).read_bytes() + b"\n# extra\n")
    monkeypatch.setattr(provider, "__file__", str(path))
    after = evidence.server_code_evidence(provider)
    assert after["status"] == "verified"
    before_hashes, after_hashes = before["source_hashes"], after["source_hashes"]
    assert isinstance(before_hashes, dict)
    assert isinstance(after_hashes, dict)
    assert before_hashes["provider"] != after_hashes["provider"]


def test_missing_and_oversized_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unavailable and oversized files fail closed."""
    path = tmp_path / "missing.py"
    monkeypatch.setattr(provider, "__file__", str(path))
    assert evidence.server_code_evidence(provider)["status"] == "unverified"
    path.write_bytes(b" " * (evidence._MAX_SOURCE_SIZE + 1))
    assert evidence.server_code_evidence(provider)["status"] == "unverified"


def test_property_and_binding_swaps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Changed descriptors or imported bindings fail closed."""
    with monkeypatch.context() as patch:
        patch.setattr(
            control_player.AirPlayControlPlayer, "_mrp_endpoint", property(lambda _: None)
        )
        assert evidence.server_code_evidence(provider)["status"] == "unverified"
    monkeypatch.setattr(provider, "AirPlayControlPlayer", object)
    assert evidence.server_code_evidence(provider)["status"] == "unverified"


def test_stale_inherited_player_class(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject a reloaded player class while the control class inherits the old one."""
    replacement = type("AirPlayPlayer", (player.AirPlayPlayer,), {})
    monkeypatch.setattr(player, "AirPlayPlayer", replacement)
    assert evidence.server_code_evidence(provider)["status"] == "unverified"


def test_stale_mro_with_matching_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject a stale control base even when both imported aliases agree."""
    replacement = type("AirPlayPlayer", (player.AirPlayPlayer,), {})
    monkeypatch.setattr(player, "AirPlayPlayer", replacement)
    monkeypatch.setattr(control_player, "AirPlayPlayer", replacement)
    assert evidence.server_code_evidence(provider)["status"] == "unverified"


def test_shadowed_inherited_getter() -> None:
    """Reject a control class override of the checked base capability property."""
    cls = control_player.AirPlayControlPlayer
    assert "_is_airplay2_capable" not in vars(cls)
    type.__setattr__(cls, "_is_airplay2_capable", property(lambda _: False))
    try:
        assert evidence.server_code_evidence(provider)["status"] == "unverified"
    finally:
        type.__delattr__(cls, "_is_airplay2_capable")


def test_unsupported_decorator_and_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject unsupported decorators and source defaults even if live code is unchanged."""
    source = Path(provider.__file__).read_bytes()
    path = tmp_path / "provider.py"
    monkeypatch.setattr(provider, "__file__", str(path))
    decorated = source.replace(
        b"    async def get_private_trial_admission_preflight(",
        b"    @staticmethod\n    async def get_private_trial_admission_preflight(",
    )
    assert decorated != source
    path.write_bytes(decorated)
    assert evidence.server_code_evidence(provider)["status"] == "unverified"
    defaulted = source.replace(
        b"self, player_id: str\n    ) -> dict[str, SerializableType]:",
        b"self, player_id: str = ''\n    ) -> dict[str, SerializableType]:",
    )
    assert defaulted != source
    path.write_bytes(defaulted)
    assert evidence.server_code_evidence(provider)["status"] == "unverified"


@pytest.mark.parametrize(
    ("owner", "name", "replacement"),
    [
        (provider, "trial_binary", object()),
        (control_player, "trial_binary", object()),
        (trial_binary, "_is_trial_target", lambda: False),
        (control_player, "supports_mrp_service", lambda _: False),
        (provider, "_PRIVATE_CREDENTIAL_KEYS", frozenset()),
        (provider, "_PRIVATE_PLAYER_ID", None),
        (constants, "CONF_MRP_CREDENTIALS", "wrong"),
        (provider, "_PRIVATE_FLAG_KEYS", ("a", "b", "c")),
        (provider, "_PRIVATE_FLAG_KEYS", provider._PRIVATE_FLAG_KEYS[:2]),
        (provider, "_PRIVATE_FLAG_STATES", ("absent", "true", "false", "null")),
        (provider, "_PRIVATE_INSTANCE_ID", None),
        (provider, "_PRIVATE_INSTANCE_ID", re.compile(r".*")),
        (provider, "CONF_PROVIDERS", "core"),
        (provider, "CONF_CORE", "providers"),
        (provider, "CONF_PRIVATE_TRIAL_TARGET", "other"),
        (provider, "asyncio", object()),
        (constants, "CONF_PRIVATE_NATIVE_VOLUME_CAPTURE", "wrong"),
    ],
)
def test_binding_inventory_swaps(
    monkeypatch: pytest.MonkeyPatch, owner: object, name: str, replacement: object
) -> None:
    """Selected imported aliases, credential constants and inventory fail closed."""
    monkeypatch.setattr(owner, name, replacement)
    assert evidence.server_code_evidence(provider)["status"] == "unverified"


def test_stale_control_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Changed MRP endpoint semantics on disk reject the old live getter."""
    source = Path(control_player.__file__).read_bytes()
    changed = source.replace(b"return self.mrp_discovery_info, Protocol.MRP", b"return None")
    assert changed != source
    path = tmp_path / "control_player.py"
    path.write_bytes(changed)
    monkeypatch.setattr(control_player, "__file__", str(path))
    assert evidence.server_code_evidence(provider)["status"] == "unverified"


def test_nested_filename_normalization() -> None:
    """Only filenames, including nested code filenames, are normalized."""
    first = compile("def f():\n return lambda: 1\n", "one", "exec")
    second = compile("def f():\n return lambda: 1\n", "two", "exec")
    changed = compile("def f():\n return lambda: 2\n", "two", "exec")
    assert evidence._normalize(first) == evidence._normalize(second)
    assert evidence._normalize(first) != evidence._normalize(changed)


_FLAG_METHODS = (
    "private_airplay_flags",
    "_private_flag_snapshot",
    "_private_target_unset",
    "_register_private_capture_api",
)
_FLAG_FUNCTIONS = (
    "_private_flag_states",
    "_private_flag_map",
    "_private_flag_request",
    "_private_flag_response",
)


@pytest.mark.parametrize("name", _FLAG_METHODS)
def test_swapped_flag_method_fails_closed(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """A live flag handler or guard that differs from its source is never verified."""
    assert evidence.server_code_evidence(provider)["status"] == "verified"
    monkeypatch.setattr(provider.AirPlayProvider, name, lambda *_args: None)
    assert evidence.server_code_evidence(provider)["status"] == "unverified"


@pytest.mark.parametrize("name", _FLAG_FUNCTIONS)
def test_swapped_flag_function_fails_closed(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """A live module level flag helper that differs from its source is never verified."""
    monkeypatch.setattr(provider, name, lambda *_args: None)
    assert evidence.server_code_evidence(provider)["status"] == "unverified"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (b'"state_mismatch"', b'"state_mismatcX"'),
        (b'desired[key] == "true"', b'desired[key] != "true"'),
        (b"if states != expected:", b"if states == expected:"),
        (b"if not self._private_target_unset():", b"if self._private_target_unset():"),
        (b"if self.get_players():", b"if not self.get_players():"),
        (b"or any(key in raw for key in _PRIVATE_FLAG_KEYS)", b"or False"),
        (b"values[key] is False", b"values[key] is not True"),
        (b"set(value) != set(_PRIVATE_FLAG_KEYS)", b"False"),
        (b'raw.get("instance_id") != instance_id', b"False"),
        (
            b"required_scope=Scope.SYSTEM_MANAGE\n                    )",
            b"required_scope=None\n                    )",
        ),
    ],
)
def test_changed_flag_source_on_disk_rejects_live_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, old: bytes, new: bytes
) -> None:
    """Mutating any flag guard or the route scope on disk rejects the loaded code."""
    source = Path(provider.__file__).read_bytes()
    assert source.count(old) >= 1
    path = tmp_path / "provider.py"
    path.write_bytes(source.replace(old, new, 1))
    monkeypatch.setattr(provider, "__file__", str(path))
    assert evidence.server_code_evidence(provider) == {"schema_version": 1, "status": "unverified"}


def test_flag_handler_decorator_and_defaults_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag handler may not gain a decorator or a source default."""
    source = Path(provider.__file__).read_bytes()
    path = tmp_path / "provider.py"
    monkeypatch.setattr(provider, "__file__", str(path))
    decorated = source.replace(
        b"    async def private_airplay_flags(",
        b"    @staticmethod\n    async def private_airplay_flags(",
    )
    assert decorated != source
    path.write_bytes(decorated)
    assert evidence.server_code_evidence(provider)["status"] == "unverified"
    defaulted = source.replace(
        b"self, request: dict[str, Any]) ->", b"self, request: dict[str, Any] = {}) ->"
    )
    assert defaulted != source
    path.write_bytes(defaulted)
    assert evidence.server_code_evidence(provider)["status"] == "unverified"
