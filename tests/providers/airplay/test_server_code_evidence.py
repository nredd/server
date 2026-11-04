"""Tests for bounded private server code evidence."""

from __future__ import annotations

import hashlib
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
