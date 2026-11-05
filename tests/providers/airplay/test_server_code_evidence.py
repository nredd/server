"""Tests for bounded private server code evidence."""

from __future__ import annotations

import ast
import enum
import hashlib
import re
from pathlib import Path
from types import FunctionType, ModuleType, SimpleNamespace
from typing import Any

import pytest

from music_assistant import mass as mass_module
from music_assistant.controllers import players as players_package
from music_assistant.controllers.players import controller as players_controller
from music_assistant.helpers.private_trial import encode_public_digest
from music_assistant.providers.airplay import (
    constants,
    control_player,
    player,
    provider,
    sendspin_bridge,
    trial_binary,
)
from music_assistant.providers.airplay import (
    server_code_evidence as evidence,
)

_STR_ENUM: Any = enum.StrEnum
_ENUM: Any = enum.Enum


def _controller() -> players_controller.PlayerController:
    """Construct an uninitialized real controller for binding checks."""
    return players_controller.PlayerController.__new__(players_controller.PlayerController)


def test_real_sources_and_raw_hash() -> None:
    """Evidence hashes the exact bytes of each selected source."""
    result = evidence.server_code_evidence(provider, _controller())
    assert result["status"] == "verified"
    hashes = result["source_hashes"]
    assert isinstance(hashes, dict)
    assert hashes["provider"] == encode_public_digest(
        hashlib.sha256(Path(provider.__file__).read_bytes()).hexdigest()
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("package", "Stale players package alias"),
        ("mass", "Stale mass controller alias"),
        ("instance", "Unexpected player controller instance"),
        ("descriptor", "Shadowed unregister"),
        ("unregister_globals", "Unexpected unregister globals"),
        ("bridge_globals", "Unexpected bridge globals"),
        ("cast", "Unexpected bridge cast"),
        ("closure", "Unexpected bridge closure"),
    ],
)
def test_runtime_binding_guard(
    monkeypatch: pytest.MonkeyPatch, mutation: str, message: str
) -> None:
    """Each runtime guard rejects its own mutation independently of the manifest."""
    controller: Any = _controller()
    evidence._check_runtime_bindings(controller)
    cls = sendspin_bridge.SendspinBridgeManager
    bridge = cls._lifecycle_allows_bridge
    unregister = players_controller.PlayerController.unregister
    if mutation == "package":
        monkeypatch.setattr(players_package, "PlayerController", object)
    elif mutation == "mass":
        monkeypatch.setattr(mass_module, "PlayerController", object)
    elif mutation == "instance":
        controller = object()
    elif mutation == "descriptor":
        controller.unregister = lambda *_args: None
    elif mutation == "unregister_globals":
        replacement = FunctionType(unregister.__code__, dict(unregister.__globals__))
        monkeypatch.setattr(players_controller.PlayerController, "unregister", replacement)
    elif mutation == "bridge_globals":
        replacement = FunctionType(
            bridge.__code__, dict(bridge.__globals__), closure=bridge.__closure__
        )
        monkeypatch.setattr(cls, "_lifecycle_allows_bridge", replacement)
    elif mutation == "cast":
        monkeypatch.setattr(sendspin_bridge, "cast", lambda *_args: None)
    else:

        def capture(value: object) -> Any:
            return lambda: value

        cell = capture(object()).__closure__
        replacement = FunctionType(bridge.__code__, bridge.__globals__, closure=cell)
        monkeypatch.setattr(cls, "_lifecycle_allows_bridge", replacement)
    with pytest.raises(ValueError, match=message):
        evidence._check_runtime_bindings(controller)


def test_unregister_defaults_require_singleton_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """An equal but noncanonical default must not verify."""
    monkeypatch.setattr(
        players_controller.PlayerController.unregister, "__defaults__", (0, None, None)
    )
    assert evidence.server_code_evidence(provider, _controller())["status"] == "unverified"


def test_wrong_provider_module() -> None:
    """A wrong module argument cannot produce verified evidence."""
    assert evidence.server_code_evidence(control_player, _controller()) == {
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
    assert evidence.server_code_evidence(provider, _controller()) == {
        "schema_version": 1,
        "status": "unverified",
    }


def test_nonsemantic_raw_change_changes_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A comment changes the raw hash even when selected live code is unchanged."""
    before = evidence.server_code_evidence(provider, _controller())
    path = tmp_path / "provider.py"
    path.write_bytes(Path(provider.__file__).read_bytes() + b"\n# extra\n")
    monkeypatch.setattr(provider, "__file__", str(path))
    after = evidence.server_code_evidence(provider, _controller())
    assert after["status"] == "verified"
    before_hashes, after_hashes = before["source_hashes"], after["source_hashes"]
    assert isinstance(before_hashes, dict)
    assert isinstance(after_hashes, dict)
    assert before_hashes["provider"] != after_hashes["provider"]


def test_missing_and_oversized_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unavailable and oversized files fail closed."""
    path = tmp_path / "missing.py"
    monkeypatch.setattr(provider, "__file__", str(path))
    assert evidence.server_code_evidence(provider, _controller())["status"] == "unverified"
    path.write_bytes(b" " * (evidence._MAX_SOURCE_SIZE + 1))
    assert evidence.server_code_evidence(provider, _controller())["status"] == "unverified"


def test_property_and_binding_swaps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Changed descriptors or imported bindings fail closed."""
    with monkeypatch.context() as patch:
        patch.setattr(
            control_player.AirPlayControlPlayer, "_mrp_endpoint", property(lambda _: None)
        )
        assert evidence.server_code_evidence(provider, _controller())["status"] == "unverified"
    monkeypatch.setattr(provider, "AirPlayControlPlayer", object)
    assert evidence.server_code_evidence(provider, _controller())["status"] == "unverified"


def test_stale_inherited_player_class(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject a reloaded player class while the control class inherits the old one."""
    replacement = type("AirPlayPlayer", (player.AirPlayPlayer,), {})
    monkeypatch.setattr(player, "AirPlayPlayer", replacement)
    assert evidence.server_code_evidence(provider, _controller())["status"] == "unverified"


def test_stale_mro_with_matching_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject a stale control base even when both imported aliases agree."""
    replacement = type("AirPlayPlayer", (player.AirPlayPlayer,), {})
    monkeypatch.setattr(player, "AirPlayPlayer", replacement)
    monkeypatch.setattr(control_player, "AirPlayPlayer", replacement)
    assert evidence.server_code_evidence(provider, _controller())["status"] == "unverified"


def test_shadowed_inherited_getter() -> None:
    """Reject a control class override of the checked base capability property."""
    cls = control_player.AirPlayControlPlayer
    assert "_is_airplay2_capable" not in vars(cls)
    type.__setattr__(cls, "_is_airplay2_capable", property(lambda _: False))
    try:
        assert evidence.server_code_evidence(provider, _controller())["status"] == "unverified"
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
    assert evidence.server_code_evidence(provider, _controller())["status"] == "unverified"
    defaulted = source.replace(
        b"self, player_id: str\n    ) -> PrivateAdmissionSnapshot:",
        b"self, player_id: str = ''\n    ) -> PrivateAdmissionSnapshot:",
    )
    assert defaulted != source
    path.write_bytes(defaulted)
    assert evidence.server_code_evidence(provider, _controller())["status"] == "unverified"


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
        (provider, "cast", lambda *_args: None),
        (provider, "Scope", SimpleNamespace(SYSTEM_MANAGE=None)),
        (constants, "CONF_PRIVATE_NATIVE_VOLUME_CAPTURE", "wrong"),
    ],
)
def test_binding_inventory_swaps(
    monkeypatch: pytest.MonkeyPatch, owner: object, name: str, replacement: object
) -> None:
    """Selected imported aliases, credential constants and inventory fail closed."""
    monkeypatch.setattr(owner, name, replacement)
    assert evidence.server_code_evidence(provider, _controller())["status"] == "unverified"


@pytest.mark.parametrize(
    ("module", "old", "new"),
    [
        (
            provider,
            b"if self.unloading or not self._admits_private_player(player_id):",
            b"if False:",
        ),
        (
            provider,
            b"await self.mass.players.register(player)\n        if self.unloading or not self._admits_private_player(player_id):",
            b"await self.mass.players.register(player)\n        if False:",
        ),
        (
            provider,
            b"await self.mass.players.unregister(player_id, expected_player=player)",
            b"await self.mass.players.unregister(player_id)",
        ),
        (
            players_controller,
            b"if expected_player is not None and self._players.get(player_id) is not player:",
            b"if False:",
        ),
        (
            sendspin_bridge,
            b"and provider._admits_private_player(player.player_id)",
            b"and True",
        ),
        (
            player,
            b"if prov.unloading or not prov._admits_private_player(self.player_id):",
            b"if False:",
        ),
        (
            control_player,
            b"if provider.unloading or not provider._admits_private_player(self.player_id):",
            b"if False:",
        ),
    ],
)
def test_registration_guard_source_mutation_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: ModuleType, old: bytes, new: bytes
) -> None:
    """Changed registration guards on disk cannot attest the loaded code."""
    assert module.__file__ is not None
    source = Path(module.__file__).read_bytes()
    assert old in source
    path = tmp_path / "changed.py"
    path.write_bytes(source.replace(old, new, 1))
    monkeypatch.setattr(module, "__file__", str(path))
    assert evidence.server_code_evidence(provider, _controller())["status"] == "unverified"


@pytest.mark.parametrize(
    ("module", "owner", "name", "old", "new", "defaults"),
    [
        (
            players_controller,
            players_controller.PlayerController,
            "unregister",
            b"if expected_player is not None and self._players.get(player_id) is not player:",
            b"if False:",
            (False, None, None),
        ),
        (
            sendspin_bridge,
            sendspin_bridge.SendspinBridgeManager,
            "_lifecycle_allows_bridge",
            b"and provider._admits_private_player(player.player_id)",
            b"and True",
            (),
        ),
    ],
)
def test_selected_source_guard_in_isolation(
    module: ModuleType,
    owner: type,
    name: str,
    old: bytes,
    new: bytes,
    defaults: tuple[object, ...],
) -> None:
    """A guard mutation fails its selected callable check without any manifest check."""
    assert module.__file__ is not None
    source = Path(module.__file__).read_bytes()
    assert source.count(old) == 1
    compiled = evidence._codes(compile(source, str(module.__file__), "exec"))
    evidence._check_function(compiled, ast.parse(source), owner, name, defaults=defaults)
    changed = source.replace(old, new)
    compiled = evidence._codes(compile(changed, str(module.__file__), "exec"))
    with pytest.raises(ValueError, match="Code mismatch"):
        evidence._check_function(compiled, ast.parse(changed), owner, name, defaults=defaults)


def test_stale_control_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Changed MRP endpoint semantics on disk reject the old live getter."""
    source = Path(control_player.__file__).read_bytes()
    changed = source.replace(b"return self.mrp_discovery_info, Protocol.MRP", b"return None")
    assert changed != source
    path = tmp_path / "control_player.py"
    path.write_bytes(changed)
    monkeypatch.setattr(control_player, "__file__", str(path))
    assert evidence.server_code_evidence(provider, _controller())["status"] == "unverified"


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
    "_private_flag_refusal",
)


@pytest.mark.parametrize("name", _FLAG_METHODS)
def test_swapped_flag_method_fails_closed(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """A live flag handler or guard that differs from its source is never verified."""
    assert evidence.server_code_evidence(provider, _controller())["status"] == "verified"
    monkeypatch.setattr(provider.AirPlayProvider, name, lambda *_args: None)
    assert evidence.server_code_evidence(provider, _controller())["status"] == "unverified"


@pytest.mark.parametrize("name", _FLAG_FUNCTIONS)
def test_swapped_flag_function_fails_closed(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """A live module level flag helper that differs from its source is never verified."""
    monkeypatch.setattr(provider, name, lambda *_args: None)
    assert evidence.server_code_evidence(provider, _controller())["status"] == "unverified"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (b"PrivateFlagReason.STATE_MISMATCH", b"PrivateFlagReason.PLAYERS_ACTIVE"),
        (b"desired[key] == PrivateFlagState.TRUE", b"desired[key] != PrivateFlagState.TRUE"),
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
    assert evidence.server_code_evidence(provider, _controller()) == {
        "schema_version": 1,
        "status": "unverified",
    }


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (b'not _PRIVATE_PLAYER_ID.fullmatch(request["player_id"])', b"False"),
        (b'target.player_id != request["player_id"]', b"False"),
        (b"self.config.enabled is not True", b"False"),
        (b"values[CONF_PRIVATE_TRIAL_TARGET] != expected or entry.value != expected", b"False"),
        (b"any(self.config.get_value(key) is not False for key in _PRIVATE_FLAG_KEYS)", b"False"),
        (b"or self.get_players()\n", b"or False\n"),
        (
            b'any(player.player_id != request["player_id"] for player in self.get_players())',
            b"False",
        ),
    ],
)
def test_admission_guard_removal_fails_function_guard(old: bytes, new: bytes) -> None:
    """Each changed admission guard is rejected by callable verification itself."""
    source = Path(provider.__file__).read_bytes()
    assert source.count(old) == 1
    changed = source.replace(old, new)
    tree = ast.parse(changed)
    codes = evidence._codes(compile(changed, "provider.py", "exec"))
    with pytest.raises(ValueError, match="Code mismatch"):
        evidence._check_function(
            codes, tree, provider.AirPlayProvider, "private_trial_admission_mutate"
        )


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
    assert evidence.server_code_evidence(provider, _controller())["status"] == "unverified"
    defaulted = source.replace(b"self, request: Any) ->", b"self, request: Any = None) ->")
    assert defaulted != source
    path.write_bytes(defaulted)
    assert evidence.server_code_evidence(provider, _controller())["status"] == "unverified"


def _constants_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, old: bytes, new: bytes
) -> None:
    """Point the loaded constants module at a mutated copy of its source."""
    source = Path(constants.__file__).read_bytes()
    assert source.count(old) == 1
    path = tmp_path / "constants.py"
    path.write_bytes(source.replace(old, new))
    monkeypatch.setattr(constants, "__file__", str(path))


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (b'STATE_MISMATCH = "state_mismatch"', b'STATE_MISMATCH = "state_mismatcX"'),
        (b'    TRUE = "true"\n', b'    TRUE = "yes"\n'),
        (b'    NEXT_LOAD = "next_load"\n', b'    NEXT_LOAD = "next_load"\n    EXTRA = "extra"\n'),
        (b'    FALSE = "false"\n', b'    FALSE = "false"\n    OTHER = "false"\n'),
        (b'    INSPECTED = "inspected"\n', b'    INSPECTED = ("inspected")\n    NEW = f"x"\n'),
        (b"class PrivateFlagMode(StrEnum):", b"class PrivateFlagMode(str):"),
        (
            b'    COMPARE_AND_SET = "compare_and_set"\n',
            b'    COMPARE_AND_SET = "compare_and_set"\n\n    def __str__(self) -> str:\n'
            b'        return "x"\n',
        ),
        (
            b"from enum import IntEnum, StrEnum",
            b"from enum import IntEnum\nfrom enum import StrEnum as E",
        ),
    ],
)
def test_changed_enum_declaration_on_disk_is_unverified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, old: bytes, new: bytes
) -> None:
    """A source enum declaration that differs from the live enum is never verified."""
    assert evidence.server_code_evidence(provider, _controller())["status"] == "verified"
    _constants_on_disk(tmp_path, monkeypatch, old, new)
    assert evidence.server_code_evidence(provider, _controller()) == {
        "schema_version": 1,
        "status": "unverified",
    }


def test_live_enum_substitutions_are_unverified(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replacing a live enum, its members or its shape fails closed."""
    cls = constants.PrivateFlagReason
    other = _STR_ENUM("PrivateFlagReason", {m.name: m.value for m in cls})
    other.__module__ = constants.__name__
    with monkeypatch.context() as patch:
        patch.setattr(provider, "PrivateFlagReason", other)
        assert evidence.server_code_evidence(provider, _controller())["status"] == "unverified"
    renamed = _STR_ENUM("PrivateFlagReason", {**{m.name: m.value for m in cls}, "X": "x"})
    renamed.__module__ = constants.__name__
    with monkeypatch.context() as patch:
        patch.setattr(constants, "PrivateFlagReason", renamed)
        patch.setattr(provider, "PrivateFlagReason", renamed)
        assert evidence.server_code_evidence(provider, _controller())["status"] == "unverified"
    plain = _ENUM("PrivateFlagReason", {m.name: m.value for m in cls}, type=str)
    with monkeypatch.context() as patch:
        patch.setattr(constants, "PrivateFlagReason", plain)
        patch.setattr(provider, "PrivateFlagReason", plain)
        assert evidence.server_code_evidence(provider, _controller())["status"] == "unverified"
    with monkeypatch.context() as patch:
        patch.setattr(constants, "StrEnum", enum.Enum)
        assert evidence.server_code_evidence(provider, _controller())["status"] == "unverified"
    assert evidence.server_code_evidence(provider, _controller())["status"] == "verified"


def test_live_enum_method_injection_is_unverified() -> None:
    """A method injected into a live enum after import is rejected."""
    cls = constants.PrivateFlagState
    type.__setattr__(cls, "__str__", lambda _self: "x")
    try:
        assert evidence.server_code_evidence(provider, _controller())["status"] == "unverified"
    finally:
        type.__setattr__(cls, "__str__", enum.StrEnum.__str__)
    assert evidence.server_code_evidence(provider, _controller())["status"] == "verified"


@pytest.mark.parametrize("attribute", ["_value2member_map_", "_member_names_", "TRUE"])
def test_live_enum_lookup_corruption_is_unverified(attribute: str) -> None:
    """Corrupting the lookup tables or a member attribute of a live enum is rejected."""
    cls = constants.PrivateFlagState
    original = vars(cls)[attribute]
    if attribute == "_value2member_map_":
        replacement: object = {**original, "true": cls.FALSE}
    elif attribute == "_member_names_":
        replacement = list(reversed(original))
    else:
        replacement = cls.FALSE
    type.__setattr__(cls, attribute, replacement)
    try:
        assert evidence.server_code_evidence(provider, _controller())["status"] == "unverified"
    finally:
        type.__setattr__(cls, attribute, original)
    assert evidence.server_code_evidence(provider, _controller())["status"] == "verified"


def test_live_enum_member_name_corruption_is_unverified() -> None:
    """A member whose stored name no longer matches its declaration is rejected."""
    member = constants.PrivateFlagState.TRUE
    member._name_ = "X"
    try:
        assert evidence.server_code_evidence(provider, _controller())["status"] == "unverified"
    finally:
        member._name_ = "TRUE"
    assert evidence.server_code_evidence(provider, _controller())["status"] == "verified"


def test_live_enum_hashable_values_corruption_is_unverified() -> None:
    """A corrupted value list of a live enum is rejected."""
    cls = constants.PrivateFlagState
    original = vars(cls)["_hashable_values_"]
    type.__setattr__(cls, "_hashable_values_", [*original, "extra"])
    try:
        assert evidence.server_code_evidence(provider, _controller())["status"] == "unverified"
    finally:
        type.__setattr__(cls, "_hashable_values_", original)
    assert evidence.server_code_evidence(provider, _controller())["status"] == "verified"


def test_live_enum_name_corruption_is_unverified() -> None:
    """A live enum whose `__name__` differs from its declaration is rejected."""
    cls = constants.PrivateFlagState
    type.__setattr__(cls, "__name__", "Other")
    try:
        assert evidence.server_code_evidence(provider, _controller())["status"] == "unverified"
    finally:
        type.__setattr__(cls, "__name__", "PrivateFlagState")
    assert evidence.server_code_evidence(provider, _controller())["status"] == "verified"
