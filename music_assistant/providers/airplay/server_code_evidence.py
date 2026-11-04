"""Bounded source and selected live-code evidence for private readiness."""

from __future__ import annotations

import ast
import hashlib
import inspect
import re
import sys
from pathlib import Path
from types import CodeType, FunctionType, ModuleType
from typing import TYPE_CHECKING

from music_assistant.helpers import private_trial
from music_assistant.helpers.private_trial import encode_public_digest

from . import constants, control_player, helpers, player, trial_binary

if TYPE_CHECKING:
    from music_assistant.helpers.json import SerializableType

_MAX_SOURCE_SIZE = 1024 * 1024


def _normalize(code: CodeType) -> CodeType:
    """Remove only source filenames, recursively, before same-interpreter comparison."""
    return code.replace(
        co_filename="",
        co_consts=tuple(
            _normalize(item) if isinstance(item, CodeType) else item for item in code.co_consts
        ),
    )


def _codes(code: CodeType) -> list[CodeType]:
    """Collect nested code for later qualified-name lookup."""
    result: list[CodeType] = []
    for item in code.co_consts:
        if isinstance(item, CodeType):
            result.append(item)
            result.extend(_codes(item))
    return result


def _check_function(
    compiled: list[CodeType], tree: ast.Module, owner: object, name: str, prop: bool = False
) -> None:
    """Check one undecorated function or explicitly expected property getter."""
    class_name = owner.__name__ if isinstance(owner, type) else None
    nodes = tree.body
    if class_name is not None:
        classes = [
            item for item in nodes if isinstance(item, ast.ClassDef) and item.name == class_name
        ]
        if len(classes) != 1:
            raise ValueError("Unexpected class")
        nodes = classes[0].body
    declarations = [
        item
        for item in nodes
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name
    ]
    if len(declarations) != 1:
        raise ValueError("Unexpected declaration")
    declaration = declarations[0]
    if declaration.args.defaults or any(item is not None for item in declaration.args.kw_defaults):
        raise ValueError("Unexpected source defaults")
    decorators = declaration.decorator_list
    if (not prop and decorators) or (
        prop
        and (
            len(decorators) != 1
            or not isinstance(decorators[0], ast.Name)
            or decorators[0].id != "property"
        )
    ):
        raise ValueError("Unexpected decorator")
    value = inspect.getattr_static(owner, name)
    if prop:
        if type(value) is not property or value.fset is not None or value.fdel is not None:
            raise ValueError("Unexpected descriptor")
        value = value.fget
    if type(value) is not FunctionType or hasattr(value, "__wrapped__"):
        raise ValueError("Unexpected callable")
    if value.__defaults__ is not None or value.__kwdefaults__ is not None:
        raise ValueError("Unexpected defaults")
    qualname = f"{class_name}.{name}" if class_name else name
    if value.__qualname__ != qualname:
        raise ValueError("Unexpected qualified name")
    matches = [item for item in compiled if item.co_qualname == qualname]
    if len(matches) != 1 or _normalize(value.__code__) != _normalize(matches[0]):
        raise ValueError("Code mismatch")


def server_code_evidence(provider: ModuleType) -> dict[str, SerializableType]:
    """Return source hashes only when selected live code and bindings agree."""
    failure: dict[str, SerializableType] = {"schema_version": 1, "status": "unverified"}
    try:
        modules = {
            "provider": provider,
            "control_player": control_player,
            "player": player,
            "helpers": helpers,
            "constants": constants,
            "trial_binary": trial_binary,
            "private_trial": private_trial,
            "server_code_evidence": sys.modules[__name__],
        }
        hashes: dict[str, SerializableType] = {}
        codes: dict[str, list[CodeType]] = {}
        trees: dict[str, ast.Module] = {}
        for key, module in modules.items():
            path = Path(str(module.__file__))
            with path.open("rb") as source:
                data = source.read(_MAX_SOURCE_SIZE + 1)
            if len(data) > _MAX_SOURCE_SIZE:
                return failure
            hashes[key] = encode_public_digest(hashlib.sha256(data).hexdigest())
            codes[key] = _codes(
                compile(data, str(path), "exec", dont_inherit=True, optimize=sys.flags.optimize)
            )
            trees[key] = ast.parse(data)
        for name in (
            "get_private_trial_admission_preflight",
            "_setup_player",
            "_admits_private_player",
        ):
            _check_function(codes["provider"], trees["provider"], provider.AirPlayProvider, name)
        for name in ("_connect_mrp",):
            _check_function(
                codes["control_player"],
                trees["control_player"],
                control_player.AirPlayControlPlayer,
                name,
            )
        for name in (
            "implicit_receiver_writes_blocked",
            "_mrp_endpoint",
            "_mrp_credentials_key",
            "_mrp_credentials",
            "_uses_transient_mrp",
            "_is_apple_tv_device",
        ):
            _check_function(
                codes["control_player"],
                trees["control_player"],
                control_player.AirPlayControlPlayer,
                name,
                prop=True,
            )
        _check_function(
            codes["player"],
            trees["player"],
            player.AirPlayPlayer,
            "_is_airplay2_capable",
            prop=True,
        )
        for name in (
            "supports_mrp_tunnel",
            "supports_transient_mrp",
            "supports_mrp_service",
            "parse_airplay_features",
            "get_decoded_property",
        ):
            _check_function(codes["helpers"], trees["helpers"], helpers, name)
        for name in ("is_trial_target", "admits_player_id"):
            _check_function(codes["private_trial"], trees["private_trial"], private_trial, name)
        _check_function(
            codes["private_trial"],
            trees["private_trial"],
            private_trial.TrialTarget,
            "player_id",
            prop=True,
        )
        _check_function(
            codes["trial_binary"], trees["trial_binary"], trial_binary, "is_trial_target"
        )
        if provider.AirPlayControlPlayer is not control_player.AirPlayControlPlayer:
            return failure
        if (
            provider.trial_binary is not trial_binary
            or vars(control_player)["trial_binary"] is not trial_binary
        ):
            return failure
        if vars(trial_binary)["_is_trial_target"] is not private_trial.is_trial_target:
            return failure
        if (
            provider.is_trial_target is not private_trial.is_trial_target
            or provider.admits_player_id is not private_trial.admits_player_id
        ):
            return failure
        for name in (
            "supports_mrp_tunnel",
            "supports_transient_mrp",
            "supports_mrp_service",
            "get_decoded_property",
        ):
            if getattr(control_player, name) is not getattr(helpers, name):
                return failure
        credential_names = (
            "CONF_AIRPLAY_CREDENTIALS",
            "CONF_RAOP_CREDENTIALS",
            "CONF_COMPANION_CREDENTIALS",
            "CONF_MRP_CREDENTIALS",
            "CONF_NATIVE_MRP_CREDENTIALS",
            "CONF_PASSWORD",
        )
        for name in credential_names:
            declarations = [
                node
                for node in trees["constants"].body
                if isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == name
            ]
            if len(declarations) != 1 or declarations[0].value is None:
                return failure
            expected = ast.literal_eval(declarations[0].value)
            if (
                not isinstance(expected, str)
                or getattr(provider, name) != expected
                or getattr(constants, name) != expected
            ):
                return failure
            if hasattr(control_player, name) and getattr(control_player, name) != expected:
                return failure
        if (
            frozenset(getattr(constants, name) for name in credential_names)
            != provider._PRIVATE_CREDENTIAL_KEYS
        ):
            return failure
        if (
            type(provider._PRIVATE_PLAYER_ID) is not re.Pattern
            or provider._PRIVATE_PLAYER_ID.pattern != r"ap[0-9a-f]{12}"
            or provider._PRIVATE_PLAYER_ID.flags != re.UNICODE
        ):
            return failure
        return {"schema_version": 1, "status": "verified", "source_hashes": hashes}
    except (
        OSError,
        ValueError,
        TypeError,
        AttributeError,
        KeyError,
        SyntaxError,
        RecursionError,
        OverflowError,
    ):
        return failure
