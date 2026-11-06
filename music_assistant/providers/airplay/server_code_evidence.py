"""Bounded source and selected live-code evidence for private readiness."""

from __future__ import annotations

import ast
import asyncio
import builtins
import contextlib
import contextvars
import dataclasses
import enum
import errno
import hashlib
import inspect
import ipaddress
import json
import logging
import os
import re
import secrets
import signal
import stat
import sys
import typing
import urllib.parse
from pathlib import Path
from types import CodeType, FunctionType, ModuleType
from typing import TYPE_CHECKING, Any, Final

from music_assistant_models.auth import Scope
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import QueueOption

from music_assistant import constants as core_constants
from music_assistant import mass as mass_module
from music_assistant.controllers import players as players_package
from music_assistant.controllers.player_queues import controller as queue_controller
from music_assistant.controllers.player_queues import helpers as queue_helpers
from music_assistant.controllers.player_queues import queue_loader
from music_assistant.controllers.player_queues.controller import PlayerQueuesController
from music_assistant.controllers.players import controller as players_controller
from music_assistant.helpers import ffmpeg, private_transport, private_trial
from music_assistant.helpers import process as process_module
from music_assistant.helpers.private_trial import encode_public_digest

from . import (
    constants,
    control_player,
    helpers,
    player,
    sendspin_bridge,
    stream,
    stream_session,
    trial_binary,
)

if TYPE_CHECKING:
    from music_assistant.helpers.json import SerializableType

_MAX_SOURCE_SIZE = 1024 * 1024
_PRIVATE_FLAG_ENUMS = (
    "PrivateAdmissionConfigState",
    "PrivateAdmissionMode",
    "PrivateAdmissionStatus",
    "PrivateAdmissionReason",
    "PrivateFlagState",
    "PrivateFlagMode",
    "PrivateFlagStatus",
    "PrivateFlagReason",
    "PrivateFlagMemory",
    "PrivateFlagPersistence",
    "PrivateFlagEffect",
)
_ENUM_FACTORY: Any = enum.StrEnum
_ENUM_SHARED_ATTRIBUTES = (
    "__new__",
    "__str__",
    "__format__",
    "_new_member_",
    "_member_type_",
    "_use_args_",
    "_value_repr_",
)


def server_code_evidence(
    provider: ModuleType, controller: players_controller.PlayerController
) -> dict[str, SerializableType]:
    """
    Return source hashes only when selected live code and bindings agree.

    :param provider: The loaded AirPlay provider module.
    :param controller: The player controller used by the provider.
    :return: A verified evidence payload or a secret-free unverified result.
    """
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
            "players_controller": players_controller,
            "sendspin_bridge": sendspin_bridge,
            "private_transport": private_transport,
            "queue_helpers": queue_helpers,
            "stream_session": stream_session,
            "stream": stream,
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
            _check_module_namespace(module, trees[key])
        for name in (
            "private_trial_transport",
            "private_trial_transport_v2",
            "_private_trial_transport",
            "_private_transport_current",
            "get_private_trial_admission_preflight",
            "_private_admission_snapshot",
            "private_trial_admission_mutate",
            "_setup_player",
            "_admits_private_player",
            "private_airplay_flags",
            "_private_flag_snapshot",
            "_private_target_unset",
            "_register_private_capture_api",
        ):
            _check_function(codes["provider"], trees["provider"], provider.AirPlayProvider, name)
            _check_globals(inspect.getattr_static(provider.AirPlayProvider, name), provider)
        _check_function(
            codes["players_controller"],
            trees["players_controller"],
            players_controller.PlayerController,
            "unregister",
            defaults=(False, None, None),
        )
        _check_function(
            codes["sendspin_bridge"],
            trees["sendspin_bridge"],
            sendspin_bridge.SendspinBridgeManager,
            "_lifecycle_allows_bridge",
        )
        _check_runtime_bindings(controller)
        _check_transport_boundaries(modules, codes, trees)
        for name in (
            "_has_current_private_receipt",
            "_private_target_address",
            "_private_flag_states",
            "_private_flag_map",
            "_private_flag_request",
            "_private_flag_response",
            "_private_flag_refusal",
        ):
            _check_function(codes["provider"], trees["provider"], provider, name)
        for name in ("_connect_mrp", "on_config_updated"):
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
        _check_function(codes["player"], trees["player"], player.AirPlayPlayer, "on_config_updated")
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
        for name in ("is_trial_target", "admits_player_id", "parse_trial_target"):
            _check_function(codes["private_trial"], trees["private_trial"], private_trial, name)
            _check_globals(getattr(private_trial, name), private_trial)
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
        if (
            provider.AirPlayControlPlayer is not control_player.AirPlayControlPlayer
            or provider.SendspinBridgeManager is not sendspin_bridge.SendspinBridgeManager
        ):
            return failure
        if (
            vars(control_player)["AirPlayPlayer"] is not player.AirPlayPlayer
            or player.AirPlayPlayer not in control_player.AirPlayControlPlayer.__mro__
            or inspect.getattr_static(control_player.AirPlayControlPlayer, "_is_airplay2_capable")
            is not inspect.getattr_static(player.AirPlayPlayer, "_is_airplay2_capable")
        ):
            return failure
        if (
            provider.trial_binary is not trial_binary
            or vars(control_player)["trial_binary"] is not trial_binary
            or vars(player)["trial_binary"] is not trial_binary
        ):
            return failure
        if vars(trial_binary)["_is_trial_target"] is not private_trial.is_trial_target:
            return failure
        if (
            provider.is_trial_target is not private_trial.is_trial_target
            or provider.admits_player_id is not private_trial.admits_player_id
            or provider.parse_trial_target is not private_trial.parse_trial_target
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
        for name in _PRIVATE_FLAG_ENUMS:
            _check_enum(trees["constants"], constants, provider, name)
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
        if (
            provider.asyncio is not asyncio
            or provider.cast is not typing.cast
            or provider.Scope is not Scope
            or provider.CONF_PROVIDERS != "providers"
            or provider.CONF_CORE != "core"
            or provider.CONF_PROVIDERS is not core_constants.CONF_PROVIDERS
            or provider.CONF_CORE is not core_constants.CONF_CORE
            or provider.CONF_PLAYERS is not core_constants.CONF_PLAYERS
            or provider.ConfigEntry is not ConfigEntry
            or type(private_trial._MAC) is not re.Pattern
            or private_trial._MAC.pattern != r"[0-9a-f]{12}"
            or private_trial._MAC.flags != re.UNICODE
            or provider.CONF_PRIVATE_TRIAL_TARGET != private_trial.CONF_PRIVATE_TRIAL_TARGET
            or tuple(
                getattr(constants, name)
                for name in (
                    "CONF_EXPERIMENTAL_HOMEPOD_REMOTE",
                    "CONF_EXPERIMENTAL_SESSION_VOLUME",
                    "CONF_PRIVATE_NATIVE_VOLUME_CAPTURE",
                )
            )
            != provider._PRIVATE_FLAG_KEYS
            or provider._PRIVATE_FLAG_KEYS
            != (
                "experimental_homepod_remote",
                "experimental_session_volume",
                "private_native_volume_capture",
            )
            or provider._PRIVATE_FLAG_STATES != ("absent", "true", "false")
            or tuple(constants.PrivateFlagState) != provider._PRIVATE_FLAG_STATES
            or type(provider._PRIVATE_INSTANCE_ID) is not re.Pattern
            or provider._PRIVATE_INSTANCE_ID.pattern != r"[A-Za-z0-9_-]{1,128}"
            or provider._PRIVATE_INSTANCE_ID.flags != re.UNICODE
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


def _check_transport_boundaries(
    modules: dict[str, ModuleType], codes: dict[str, list[CodeType]], trees: dict[str, ast.Module]
) -> None:
    """Check actual ownership boundaries, defaults and module-global bindings."""
    owners = (
        (
            "private_transport",
            private_transport,
            (
                "validate_request",
                "parse_media_record",
                "media_qualifies",
                "resolved_media_route",
                "_reject_media_duplicates",
                "_media_int",
                "_media_reject",
                "_media_uint",
                "_media_bool",
                "is_canonical_ipv4",
            ),
        ),
        (
            "trial_binary",
            trial_binary,
            (
                "_parse_attestation_line",
                "_reject_duplicate_json_keys",
                "create_private_process",
                "_open_private_asset_fd",
            ),
        ),
        (
            "private_transport",
            private_transport.ReceiptScanner,
            ("__init__", "feed", "finish", "_complete_line"),
        ),
        (
            "trial_binary",
            trial_binary.TrialAsyncProcess,
            (
                "__init__",
                "enter_receipt_mode",
                "read_receipt_chunk",
                "reject_receipt_stream",
                "discard_receipt_stream",
                "read_stdout",
                "write",
                "verified_stdin_fd",
                "_release_after_exit",
                "_release_asset_fd",
                "read",
                "readexactly",
                "communicate",
                "start",
                "_verify_acknowledgement",
                "_read_attestation_line",
                "_current_receipt",
                "_kill_and_reap",
                "close",
                "kill",
                "has_historical_receipt",
                "has_current_receipt",
                "_release_receipt_owner",
                "_receipt_read",
                "_screen_receipt_bytes",
            ),
        ),
        (
            "private_transport",
            private_transport.PrivateTransportAttempt,
            (
                "__init__",
                "check_current",
                "bind_session",
                "bind_stream",
                "spawn",
                "claim_start",
                "start_allowed",
                "play",
                "invalidate",
                "request_stop",
                "snapshot",
                "fail_media",
                "accept_media",
                "media_snapshot",
                "_start",
                "_force",
                "_play",
                "_watchdog",
                "_cleanup",
                "_release",
                "_read_pipe",
                "_drain",
                "_hand_off_stdout",
            ),
        ),
        ("queue_helpers", queue_helpers, ("handle_play_action",)),
        ("player", player.AirPlayPlayer, ("play_media",)),
        (
            "stream_session",
            stream_session.AirPlayStreamSession,
            (
                "__init__",
                "start",
                "stop",
                "private_release",
                "_start_client",
                "_start_player_ffmpeg",
                "_write_chunk_to_all_players",
            ),
        ),
        (
            "stream",
            stream.AirPlayStream,
            (
                "__init__",
                "connect",
                "start",
                "stop",
                "private_release",
                "_write_cli_command",
                "_connect_volume_allowed",
                "_create_private_pipe",
                "_build_cli_args",
                "_read_stdout",
                "_handle_stdout_line",
                "_private_stdout_reader",
                "_private_owner_current",
                "_attach_stdout_reader",
                "_uses_private_receipt",
                "_check_private_start",
                "_parse_route_status",
            ),
        ),
    )
    for key, owner, names in owners:
        for name in names:
            _check_boundary(codes[key], trees[key], owner, name, modules[key])
    for module in (queue_helpers, player, stream_session, stream):
        if vars(module)["CURRENT_ATTEMPT"] is not private_transport.CURRENT_ATTEMPT:
            raise ValueError("Unexpected attempt context binding")
    if (
        modules["provider"].PrivateTransportAttempt is not private_transport.PrivateTransportAttempt
        or modules["provider"].validate_request is not private_transport.validate_request
        or vars(player)["AirPlayStreamSession"] is not stream_session.AirPlayStreamSession
        or vars(stream_session)["AirPlayStream"] is not stream.AirPlayStream
        or vars(stream_session)["FFMpeg"] is not ffmpeg.FFMpeg
        or vars(modules["provider"])["QueueOption"] is not QueueOption
    ):
        raise ValueError("Unexpected transport boundary binding")
    _check_transport_globals(trees["private_transport"])
    _check_receipt_bindings(modules, codes, trees)
    _check_trial_qualification(modules, codes, trees)
    for name, module, qualname in (
        ("_handle_play_media", queue_loader, "QueueLoaderMixin._handle_play_media"),
        ("play_index", queue_controller, "PlayerQueuesController.play_index"),
    ):
        _check_queue_wrapper(
            getattr(PlayerQueuesController, name), codes["queue_helpers"], module, qualname
        )


def _check_transport_globals(tree: ast.Module) -> None:
    """Reject substituted request validators and the shared ownership context."""
    if (
        vars(private_transport)["asyncio"] is not asyncio
        or vars(private_transport)["re"] is not re
        or vars(private_transport)["urlsplit"] is not urllib.parse.urlsplit
        or type(private_transport.CURRENT_ATTEMPT) is not contextvars.ContextVar
        or private_transport.CURRENT_ATTEMPT.name != "private_transport_attempt"
        or contextvars.Context().run(private_transport.CURRENT_ATTEMPT.get) is not None
    ):
        raise ValueError("Unexpected transport globals")
    for name in ("_TOKEN", "_PLAYER", "_STIMULUS_PATH"):
        declaration = next(
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == name
        )
        if not isinstance(declaration.value, ast.Call) or len(declaration.value.args) != 1:
            raise ValueError("Unexpected transport pattern declaration")
        pattern = getattr(private_transport, name)
        if (
            type(pattern) is not re.Pattern
            or pattern.pattern != ast.literal_eval(declaration.value.args[0])
            or pattern.flags != re.UNICODE
        ):
            raise ValueError("Unexpected transport pattern")


def _check_receipt_bindings(
    modules: dict[str, ModuleType],
    modules_codes: dict[str, list[CodeType]],
    trees: dict[str, ast.Module],
) -> None:
    """Seal the receipt parser's constants, shapes and imported dependencies."""
    _check_literal_globals(
        trees["private_transport"],
        private_transport,
        (
            "MEDIA_PREFIX",
            "MAX_MEDIA_RECORD",
            "MAX_STATUS_LINE",
            "MAX_STDOUT_TOTAL",
            "MEDIA_ROUTES",
            "_MEDIA_KEYS",
            "_ALL_ROUTES",
            "_CANONICAL_UINT",
        ),
    )
    _check_literal_globals(
        trees["trial_binary"], trial_binary, ("MAX_RECEIPT_CHUNK", "_ATTEST_PREFIX")
    )
    stream_globals, provider_globals = vars(stream), vars(modules["provider"])
    if (
        vars(private_transport)["json"] is not json
        or vars(private_transport)["ipaddress"] is not ipaddress
        or vars(private_transport)["dataclass"] is not dataclasses.dataclass
        or vars(private_transport)["MediaReceiptError"] is not private_transport.MediaReceiptError
        or vars(trial_binary)["suppress"] is not contextlib.suppress
        or vars(private_transport)["cast"] is not typing.cast
        or vars(private_transport)["suppress"] is not contextlib.suppress
        or vars(private_transport)["TransportOutcome"] is not private_transport.TransportOutcome
        or vars(private_transport)["MediaState"] is not private_transport.MediaState
        or stream_globals["ReceiptScanner"] is not private_transport.ReceiptScanner
        or stream_globals["resolved_media_route"] is not private_transport.resolved_media_route
        or stream_globals["PrivateTransportAttempt"]
        is not private_transport.PrivateTransportAttempt
        or stream_globals["trial_binary"] is not trial_binary
        or provider_globals["is_canonical_ipv4"] is not private_transport.is_canonical_ipv4
    ):
        raise ValueError("Unexpected receipt dependency bindings")
    _check_receipt_shapes()
    process_class = trial_binary.TrialAsyncProcess
    if (
        vars(trial_binary)["TrialAsyncProcess"] is not process_class
        or vars(trial_binary)["AsyncProcess"] is not process_module.AsyncProcess
        or process_class.__bases__ != (process_module.AsyncProcess,)
        or type(process_class) is not type
    ):
        raise ValueError("Unexpected private process class")
    if (
        set(vars(process_class)) - {"__firstlineno__", "__static_attributes__"}
        != _TRIAL_PROCESS_NAMES
    ):
        raise ValueError("Unexpected private process members")
    for name in ("receipt_rejected", "receipt_eof"):
        _check_function(
            modules_codes["trial_binary"], trees["trial_binary"], process_class, name, prop=True
        )


_TRIAL_PROCESS_NAMES: Final = frozenset(
    {
        "__module__",
        "__doc__",
        "__init__",
        "start",
        "has_historical_receipt",
        "receipt_rejected",
        "receipt_eof",
        "has_current_receipt",
        "verified_stdin_fd",
        "write",
        "read",
        "readexactly",
        "enter_receipt_mode",
        "read_receipt_chunk",
        "discard_receipt_stream",
        "reject_receipt_stream",
        "communicate",
        "read_stdout",
        "close",
        "kill",
        "_release_receipt_owner",
        "_receipt_read",
        "_screen_receipt_bytes",
        "_verify_acknowledgement",
        "_read_attestation_line",
        "_kill_and_reap",
        "_release_after_exit",
        "_release_asset_fd",
        "_current_receipt",
    }
)

# Trusted boundary for the qualification closure. Project code below is sealed by source
# comparison; only these are trusted as the Python runtime itself: builtins and the stdlib
# modules `asyncio errno hashlib json os re secrets stat sys platform` (identity-bound, but
# their attributes are not compared), `pathlib.Path`, `contextlib.suppress`,
# `dataclasses.dataclass` and `typing.cast`.
TRIAL_GLOBALS: Final = frozenset(
    {
        "ASSET_NAME",
        "ATTEST_TIMEOUT",
        "AsyncProcess",
        "CHECK_TIMEOUT",
        "ChildAttestationChangedError",
        "EXPECTED_BUILD",
        "EXPECTED_SHA",
        "MAX_ATTEST_LINE",
        "MAX_BINARY_SIZE",
        "MAX_CHECK_OUTPUT",
        "MAX_RECEIPT_CHUNK",
        "PROC_ROOT",
        "Path",
        "TRIAL_ROOT",
        "TrialAsyncProcess",
        "TrialBinaryError",
        "_ATTEST_PREFIX",
        "_BUILD_LABEL",
        "_TrustedChildReceipt",
        "_is_trial_target",
        "asyncio",
        "dataclass",
        "descriptor_path",
        "encode_public_digest",
        "errno",
        "get_subprocess_env",
        "hashlib",
        "json",
        "open_in_thread",
        "os",
        "re",
        "secrets",
        "stat",
        "suppress",
    }
    | {
        "_attestation_os_error",
        "_check_binary",
        "_check_open_descriptor",
        "_digest_fd",
        "_open_private_asset_fd",
        "_open_validated_asset",
        "_parse_attestation_line",
        "_read_child_digest",
        "_reject_duplicate_json_keys",
        "_validate_asset",
        "_validate_elf_and_digest",
        "_validate_open_asset",
        "attest_owned_child",
        "create_private_process",
        "is_trial_target",
        "preflight_current_image",
        "validate_trial_binary",
    }
)
# Project helpers the closure leaves trial_binary for, with the globals each may read.
PROCESS_HELPER_GLOBALS: Final = {
    "get_subprocess_env": frozenset({"os"}),
    "descriptor_path": frozenset({"sys"}),
    "open_in_thread": frozenset({"_close_late_descriptor", "asyncio"}),
    "_close_late_descriptor": frozenset({"os"}),
}
# Inherited `AsyncProcess` members `TrialAsyncProcess` reaches (self./super() attributes,
# transitively), the exec path included. Enforced complete by a bytecode-audit test.
ASYNC_PROCESS_METHODS: Final = frozenset(
    {
        "__init__",
        "start",
        "read",
        "write",
        "read_stdout",
        "communicate",
        "close",
        "kill",
        "wait",
        "wait_with_timeout",
        "_prepare_stderr_close",
        "_reclaim_finished_stderr_reader",
        "_wait_for_stderr_reader",
        "_readline",
        "_acquire_stream_lock",
        "_drain_pipe",
        "_drain_under_lock",
        "_cancel_and_await",
        "_is_expected_task_error",
    }
)
ASYNC_PROCESS_PROPERTIES: Final = frozenset({"closed", "returncode"})
ASYNC_PROCESS_CLASS_ATTRS: Final = frozenset({"_stdin_feeder_task", "_stderr_reader_task"})
ASYNC_PROCESS_GLOBALS: Final = frozenset(
    {
        "LOGGER",
        "PIPE_DRAIN_TIMEOUT",
        "Path",
        "SIGINT",
        "STREAM_RELEASE_TIMEOUT",
        "VERBOSE_LOG_LEVEL",
        "asyncio",
        "get_subprocess_env",
        "logging",
        "os",
        "suppress",
    }
)
_CLASS_DUNDERS: Final = frozenset(
    {
        "__module__",
        "__doc__",
        "__dict__",
        "__weakref__",
        "__firstlineno__",
        "__static_attributes__",
        "__qualname__",
        "__annotations__",
        "__annotate_func__",
        "__annotations_cache__",
        "__classdictcell__",
    }
)
PRIVATE_TRIAL_HELPER_GLOBALS: Final = {
    "encode_public_digest": frozenset({"_SHA", "_SHA256"}),
}


def _trial_constant(node: ast.expr) -> object:
    """Evaluate the closed set of constant shapes `trial_binary` declares."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        left, right = _trial_constant(node.left), _trial_constant(node.right)
        if type(left) is int and type(right) is int:
            return left * right
    if isinstance(node, ast.Call) and len(node.args) == 1 and not node.keywords:
        argument = _trial_constant(node.args[0])
        if ast.unparse(node.func) == "Path" and type(argument) is str:
            return Path(argument)
        if ast.unparse(node.func) == "re.compile" and type(argument) is str:
            return re.compile(argument)
    raise ValueError("Unexpected qualification constant shape")


def _check_trial_constants(tree: ast.Module) -> None:
    """Compare every top-level qualification constant with its source declaration."""
    for node in tree.body:
        if not (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            continue
        name = node.targets[0].id
        expected, live = _trial_constant(node.value), vars(trial_binary)[name]
        if isinstance(expected, re.Pattern):
            if (
                type(live) is not re.Pattern
                or live.pattern != expected.pattern
                or live.flags != expected.flags
            ):
                raise ValueError("Unexpected qualification pattern")
        elif type(live) is not type(expected) or live != expected:
            raise ValueError("Unexpected qualification constant")


def _check_trial_qualification(
    modules: dict[str, ModuleType], codes: dict[str, list[CodeType]], trees: dict[str, ast.Module]
) -> None:
    """Seal factory -> asset validation -> exec path -> attestation -> historical trust."""
    tree, module = trees["trial_binary"], trial_binary
    _check_trial_constants(tree)
    declared = {
        node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    imported = {
        "dataclass",
        "descriptor_path",
        "get_subprocess_env",
        "open_in_thread",
        "encode_public_digest",
    }
    live_functions = {
        name
        for name, value in vars(module).items()
        if isinstance(value, FunctionType) and name not in imported | {"_is_trial_target"}
    }
    if declared != live_functions:
        raise ValueError("Unexpected qualification functions")
    for name in sorted(declared):
        _check_boundary(codes["trial_binary"], tree, module, name, modules["trial_binary"])
    bindings = {
        "asyncio": asyncio,
        "errno": errno,
        "hashlib": hashlib,
        "json": json,
        "os": os,
        "re": re,
        "secrets": secrets,
        "stat": stat,
        "suppress": contextlib.suppress,
        "dataclass": dataclasses.dataclass,
        "Path": Path,
        "AsyncProcess": process_module.AsyncProcess,
        "descriptor_path": process_module.descriptor_path,
        "get_subprocess_env": process_module.get_subprocess_env,
        "open_in_thread": process_module.open_in_thread,
        "encode_public_digest": private_trial.encode_public_digest,
        "_is_trial_target": private_trial.is_trial_target,
    }
    live = vars(module)
    if any(live[name] is not value for name, value in bindings.items()):
        raise ValueError("Unexpected qualification bindings")
    errors = (
        (module.TrialBinaryError, RuntimeError),
        (module.ChildAttestationChangedError, ValueError),
    )
    for error, base in errors:
        if error.__bases__ != (base,) or type(error) is not type:
            raise ValueError("Unexpected qualification error class")
    receipt = module._TrustedChildReceipt
    params = getattr(receipt, "__dataclass_params__", None)
    if (
        receipt.__bases__ != (object,)
        or params is None
        or params.frozen is not True
        or tuple(getattr(receipt, "__dataclass_fields__", ())) != ("process", "proc", "digest")
    ):
        raise ValueError("Unexpected trusted receipt class")
    _check_process_helpers(codes, trees)


def _check_process_helpers(codes: dict[str, list[CodeType]], trees: dict[str, ast.Module]) -> None:
    """Seal the helper implementations the closure delegates to, against fresh compiles."""
    for name, deps in PRIVATE_TRIAL_HELPER_GLOBALS.items():
        _check_function(codes["private_trial"], trees["private_trial"], private_trial, name)
        _check_globals(getattr(private_trial, name), private_trial)
        _check_literal_globals(trees["private_trial"], private_trial, tuple(sorted(deps)))
    path = Path(str(process_module.__file__))
    with path.open("rb") as source:
        data = source.read(_MAX_SOURCE_SIZE + 1)
    if len(data) > _MAX_SOURCE_SIZE:
        raise ValueError("Oversized process helper source")
    compiled = _codes(
        compile(data, str(path), "exec", dont_inherit=True, optimize=sys.flags.optimize)
    )
    tree = ast.parse(data)
    _check_module_namespace(process_module, tree)
    for name in PROCESS_HELPER_GLOBALS:
        _check_boundary(compiled, tree, process_module, name, process_module)
    _check_async_process(compiled, tree)
    if (
        vars(process_module)["os"] is not os
        or vars(process_module)["sys"] is not sys
        or vars(process_module)["asyncio"] is not asyncio
    ):
        raise ValueError("Unexpected process helper bindings")


def _check_async_process(compiled: list[CodeType], tree: ast.Module) -> None:
    """Seal the inherited `AsyncProcess` code the wrapper's exec and close path runs."""
    base = process_module.AsyncProcess
    child = trial_binary.TrialAsyncProcess
    if child.__mro__ != (child, base, object) or base.__bases__ != (object,):
        raise ValueError("Unexpected process class hierarchy")
    if type(base) is not type or type(child) is not type:
        raise ValueError("Unexpected process metaclass")
    declaration = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == base.__name__
    )
    declared = {
        node.name
        for node in declaration.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    } | {
        node.target.id
        for node in declaration.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    if set(vars(base)) - _CLASS_DUNDERS != declared:
        raise ValueError("Unexpected process class members")
    for name in sorted(ASYNC_PROCESS_METHODS):
        _check_boundary(compiled, tree, base, name, process_module)
    for name in sorted(ASYNC_PROCESS_PROPERTIES):
        _check_function(compiled, tree, base, name, prop=True)
    if any(vars(base)[name] is not None for name in ASYNC_PROCESS_CLASS_ATTRS):
        raise ValueError("Unexpected process class attribute")
    _check_literal_globals(tree, process_module, ("PIPE_DRAIN_TIMEOUT", "STREAM_RELEASE_TIMEOUT"))
    live = vars(process_module)
    logger = live["LOGGER"]
    if (
        type(logger) is not logging.Logger
        or logger.name != f"{core_constants.MASS_LOGGER_NAME}.helpers.process"
        or live["SIGINT"] is not signal.SIGINT
        or live["VERBOSE_LOG_LEVEL"] != core_constants.VERBOSE_LOG_LEVEL
        or live["logging"] is not logging
        or live["Path"] is not Path
        or live["suppress"] is not contextlib.suppress
    ):
        raise ValueError("Unexpected process module bindings")


def _check_receipt_shapes() -> None:
    """Seal the closed receipt state enum and record dataclass."""
    record = private_transport.MediaReceipt
    if (
        [item.value for item in private_transport.MediaState]
        != ["pending", "incomplete", "complete"]
        or [item.name for item in dataclasses.fields(record)]
        != [
            "terminal",
            "route",
            "epoch",
            "fresh_units",
            "fresh_bytes",
            "idle_units",
            "backlog_units",
            "fault",
            "overflow",
            "addr",
            "port",
            "setup_port",
        ]
        or not dataclasses.is_dataclass(record)
        or vars(record)["__dataclass_params__"].frozen is not True
    ):
        raise ValueError("Unexpected receipt record shape")


def _check_literal_globals(tree: ast.Module, module: ModuleType, names: tuple[str, ...]) -> None:
    """Compare selected live constants and patterns with their source declarations."""
    for name in names:
        declarations = [
            node
            for node in tree.body
            if (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == name
            )
            or (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == name
            )
        ]
        if len(declarations) != 1 or declarations[0].value is None:
            raise ValueError("Unexpected receipt constant declaration")
        value, live = declarations[0].value, vars(module)[name]
        if isinstance(value, ast.Call):
            func = ast.unparse(value.func)
            argument = ast.literal_eval(value.args[0])
            if func == "re.compile":
                if (
                    type(live) is not re.Pattern
                    or live.pattern != argument
                    or live.flags != re.UNICODE
                ):
                    raise ValueError("Unexpected receipt pattern")
            elif func == "frozenset":
                if type(live) is not frozenset or live != frozenset(argument):
                    raise ValueError("Unexpected receipt constant")
            else:
                raise ValueError("Unexpected receipt constant call")
            continue
        expected = ast.literal_eval(value)
        if type(live) is not type(expected) or live != expected:
            raise ValueError("Unexpected receipt constant")


def _check_boundary(
    compiled: list[CodeType], tree: ast.Module, owner: object, name: str, module: ModuleType
) -> None:
    """Verify an undecorated boundary including literal positional/keyword defaults."""
    value = inspect.getattr_static(owner, name)
    if type(value) is not FunctionType or hasattr(value, "__wrapped__"):
        raise ValueError("Unexpected boundary callable")
    class_name = owner.__name__ if isinstance(owner, type) else None
    qualname = f"{class_name}.{name}" if class_name else name
    matches = [item for item in compiled if item.co_qualname == qualname]
    if len(matches) != 1 or _normalize(value.__code__) != _normalize(matches[0]):
        raise ValueError("Boundary code mismatch")
    nodes = tree.body
    if class_name is not None:
        nodes = next(
            node.body
            for node in nodes
            if isinstance(node, ast.ClassDef) and node.name == class_name
        )
    declaration = next(
        node
        for node in nodes
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )
    if declaration.decorator_list:
        raise ValueError("Unexpected boundary decorator")
    if value.__qualname__ != qualname:
        raise ValueError("Unexpected boundary qualified name")
    _check_callable_defaults(value, declaration)
    _check_globals(value, module)
    if value.__closure__ is not None and not (
        class_name is not None
        and value.__code__.co_freevars == ("__class__",)
        and len(value.__closure__) == 1
        and value.__closure__[0].cell_contents is owner
    ):
        raise ValueError("Unexpected boundary closure")


def _check_callable_defaults(
    value: FunctionType, declaration: ast.FunctionDef | ast.AsyncFunctionDef
) -> None:
    """Compare literal source defaults without accepting equal but differently typed values."""
    defaults = tuple(ast.literal_eval(item) for item in declaration.args.defaults)
    kwdefaults = {
        arg.arg: ast.literal_eval(default)
        for arg, default in zip(
            declaration.args.kwonlyargs, declaration.args.kw_defaults, strict=True
        )
        if default is not None
    }
    actual_defaults = value.__defaults__ or ()
    actual_kwdefaults = value.__kwdefaults__ or {}
    if (
        len(actual_defaults) != len(defaults)
        or any(
            type(actual) is not type(expected) or actual != expected
            for actual, expected in zip(actual_defaults, defaults, strict=True)
        )
        or set(actual_kwdefaults) != set(kwdefaults)
        or any(
            type(actual_kwdefaults[key]) is not type(expected) or actual_kwdefaults[key] != expected
            for key, expected in kwdefaults.items()
        )
    ):
        raise ValueError("Unexpected boundary defaults")


def _check_queue_wrapper(
    value: object, compiled: list[CodeType], module: ModuleType, qualname: str
) -> None:
    """Accept only the actual wraps wrapper with its exact wrapped callable cell."""
    wrapped = getattr(value, "__wrapped__", None)
    if type(value) is not FunctionType or type(wrapped) is not FunctionType:
        raise ValueError("Unexpected queue wrapper")
    expected = next(
        code for code in compiled if code.co_qualname == "handle_play_action.<locals>.wrapper"
    )
    if (
        _normalize(value.__code__) != _normalize(expected)
        or value.__globals__ is not vars(queue_helpers)
        or value.__defaults__ is not None
        or value.__kwdefaults__ is not None
        or value.__code__.co_freevars != ("func",)
        or value.__closure__ is None
        or len(value.__closure__) != 1
        or value.__closure__[0].cell_contents is not wrapped
    ):
        raise ValueError("Unexpected queue wrapper binding")
    if wrapped.__module__ != module.__name__ or wrapped.__qualname__ != qualname:
        raise ValueError("Unexpected wrapped callable identity")
    source_path = Path(str(module.__file__)).resolve()
    if not source_path.exists() or not source_path.is_file():
        raise ValueError("Missing wrapped callable source")
    with source_path.open("rb") as source_file:
        source = source_file.read(_MAX_SOURCE_SIZE + 1)
    if len(source) > _MAX_SOURCE_SIZE or wrapped.__globals__ is not vars(module):
        raise ValueError("Unexpected wrapped callable globals")
    matches = [
        code
        for code in _codes(
            compile(
                source, str(module.__file__), "exec", dont_inherit=True, optimize=sys.flags.optimize
            )
        )
        if code.co_qualname == qualname
    ]
    if len(matches) != 1 or _normalize(wrapped.__code__) != _normalize(matches[0]):
        raise ValueError("Unexpected wrapped callable code")
    declarations = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == wrapped.__name__
    ]
    if len(declarations) != 1 or wrapped.__closure__ is not None:
        raise ValueError("Unexpected wrapped callable closure")
    _check_callable_defaults(wrapped, declarations[0])


_MODULE_DUNDERS: Final = frozenset(
    {
        "__name__",
        "__doc__",
        "__package__",
        "__loader__",
        "__spec__",
        "__file__",
        "__cached__",
        "__builtins__",
        "__annotations__",
        "__conditional_annotations__",
        "__warningregistry__",
    }
)


def _bound_names(target: ast.AST) -> set[str]:
    """Return every plain name bound by an assignment-style target."""
    return {node.id for node in ast.walk(target) if isinstance(node, ast.Name)}


def _declared_names(tree: ast.Module) -> set[str]:
    """Return every module global the source binds (top-level flow, `global` and walrus)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, ast.Global):
            names.update(node.names)
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                names |= _bound_names(target)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.NamedExpr)):
            names |= _bound_names(node.target)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            names |= _bound_names(node.optional_vars)
    return names


def _check_module_namespace(module: ModuleType, tree: ast.Module) -> None:
    """
    Reject any module global the source does not declare, and a swapped builtins mapping.

    Sealed code resolves builtins (`len`, `bytes`, `isinstance`, ...) through the module's own
    globals first, so an undeclared attribute of ANY type (function, class, `partial`, bound
    method, object) would silently shadow one. The inventory is closed against the module's
    own source rather than the code being compared.

    :param module: The live sealed module.
    :param tree: The module's parsed source.
    :raises ValueError: If the live namespace holds an undeclared name or foreign builtins.
    """
    live = vars(module)
    builtins_ref = live.get("__builtins__")
    builtins_dict = builtins_ref if isinstance(builtins_ref, dict) else vars(builtins_ref)
    if builtins_dict is not vars(builtins):
        raise ValueError("Unexpected module builtins")
    if set(live) - _declared_names(tree) - _MODULE_DUNDERS:
        raise ValueError("Undeclared module global")
    if "__warningregistry__" in live and type(live["__warningregistry__"]) is not dict:
        raise ValueError("Unexpected warnings registry")


def _check_globals(function: FunctionType, module: ModuleType) -> None:
    """Reject selected functions that execute against another globals dictionary."""
    if function.__globals__ is not vars(module):
        raise ValueError("Unexpected function globals")


def _check_runtime_bindings(controller: object) -> None:
    """Reject stale consumers and noncanonical bindings of selected race guards."""
    controller_class = players_controller.PlayerController
    unregister = inspect.getattr_static(controller_class, "unregister")
    if players_package.PlayerController is not controller_class:
        raise ValueError("Stale players package alias")
    if vars(mass_module)["PlayerController"] is not controller_class:
        raise ValueError("Stale mass controller alias")
    if type(controller) is not controller_class:
        raise ValueError("Unexpected player controller instance")
    if inspect.getattr_static(controller, "unregister") is not unregister:
        raise ValueError("Shadowed unregister")
    if unregister.__globals__ is not vars(players_controller):
        raise ValueError("Unexpected unregister globals")
    bridge_class = sendspin_bridge.SendspinBridgeManager
    lifecycle = inspect.getattr_static(bridge_class, "_lifecycle_allows_bridge")
    if lifecycle.__globals__ is not vars(sendspin_bridge):
        raise ValueError("Unexpected bridge globals")
    if vars(sendspin_bridge)["cast"] is not typing.cast:
        raise ValueError("Unexpected bridge cast")
    if (
        lifecycle.__code__.co_freevars != ("__class__",)
        or lifecycle.__closure__ is None
        or len(lifecycle.__closure__) != 1
        or lifecycle.__closure__[0].cell_contents is not bridge_class
    ):
        raise ValueError("Unexpected bridge closure")


def _normalize(code: CodeType) -> CodeType:
    """Return a code object suitable for same-interpreter comparison."""
    return code.replace(
        co_filename="",
        co_consts=tuple(
            _normalize(item) if isinstance(item, CodeType) else item for item in code.co_consts
        ),
    )


def _codes(code: CodeType) -> list[CodeType]:
    """Return all nested code objects."""
    result: list[CodeType] = []
    for item in code.co_consts:
        if isinstance(item, CodeType):
            result.append(item)
            result.extend(_codes(item))
    return result


def _check_function(
    compiled: list[CodeType],
    tree: ast.Module,
    owner: object,
    name: str,
    prop: bool = False,
    defaults: tuple[object, ...] = (),
) -> None:
    """
    Reject a selected callable that does not match its source declaration.

    :param compiled: Code objects compiled from the source.
    :param tree: Parsed source declarations.
    :param owner: Module or class containing the callable.
    :param name: Expected callable name.
    :param prop: Whether the callable must be a property getter.
    :param defaults: Expected positional defaults, if any.
    :raises ValueError: If the callable differs from its source declaration.
    """
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
    if tuple(ast.literal_eval(item) for item in declaration.args.defaults) != defaults or any(
        item is not None for item in declaration.args.kw_defaults
    ):
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
    actual_defaults = value.__defaults__ or ()
    if (
        len(actual_defaults) != len(defaults)
        or any(
            actual is not expected
            for actual, expected in zip(actual_defaults, defaults, strict=True)
        )
        or value.__kwdefaults__ is not None
    ):
        raise ValueError("Unexpected defaults")
    qualname = f"{class_name}.{name}" if class_name else name
    if value.__qualname__ != qualname:
        raise ValueError("Unexpected qualified name")
    matches = [item for item in compiled if item.co_qualname == qualname]
    if len(matches) != 1 or _normalize(value.__code__) != _normalize(matches[0]):
        raise ValueError("Code mismatch")


def _check_enum(tree: ast.Module, module: ModuleType, consumer: ModuleType, name: str) -> None:
    """
    Reject a live string enum that differs from its simple literal source declaration.

    Only `class Name(StrEnum)` bodies of unique `NAME = "literal"` members are supported.

    :param tree: Parsed source of the module declaring the enum.
    :param module: The loaded module declaring the enum.
    :param consumer: The loaded module that must be bound to that same enum class.
    :param name: Expected enum class name.
    :raises ValueError: If the live enum differs from its source declaration.
    """
    imports = [
        alias
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "enum" and node.level == 0
        for alias in node.names
        if alias.name == "StrEnum" or alias.asname == "StrEnum"
    ]
    if (
        len(imports) != 1
        or imports[0].name != "StrEnum"
        or imports[0].asname is not None
        or vars(module).get("StrEnum") is not enum.StrEnum
    ):
        raise ValueError("Unexpected StrEnum binding")
    classes = [item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == name]
    if len(classes) != 1:
        raise ValueError("Unexpected enum declaration")
    declaration = classes[0]
    if (
        len(declaration.bases) != 1
        or not isinstance(declaration.bases[0], ast.Name)
        or declaration.bases[0].id != "StrEnum"
        or declaration.keywords
        or declaration.decorator_list
        or declaration.type_params
    ):
        raise ValueError("Unexpected enum base")
    declared: list[tuple[str, str]] = []
    for index, item in enumerate(declaration.body):
        if (
            index == 0
            and isinstance(item, ast.Expr)
            and isinstance(item.value, ast.Constant)
            and isinstance(item.value.value, str)
        ):
            continue
        if (
            not isinstance(item, ast.Assign)
            or len(item.targets) != 1
            or not isinstance(item.targets[0], ast.Name)
            or not isinstance(item.value, ast.Constant)
            or type(item.value.value) is not str
        ):
            raise ValueError("Unsupported enum member")
        declared.append((item.targets[0].id, item.value.value))
    if (
        not declared
        or len({member for member, _ in declared}) != len(declared)
        or len({value for _, value in declared}) != len(declared)
    ):
        raise ValueError("Unsupported enum members")
    # The source is only parsed, never run: a pristine class is built from the declared literals.
    live: Any = vars(module)[name]
    if vars(consumer)[name] is not live:
        raise ValueError("Unexpected enum binding")
    members: list[tuple[str, Any]] = list(live.__members__.items())
    reference: Any = _ENUM_FACTORY(name, declared)
    if (
        not isinstance(live, type)
        or type(live) is not enum.EnumType
        or live.__bases__ != (enum.StrEnum,)
        or live.__module__ != module.__name__
        or live.__qualname__ != name
        or live.__name__ != name
        or [(key, member.value) for key, member in members] != declared
        or any(type(member) is not live for _, member in members)
        or set(vars(live)) - {"__firstlineno__", "__static_attributes__"} != set(vars(reference))
        or any(vars(live)[key] is not vars(reference)[key] for key in _ENUM_SHARED_ATTRIBUTES)
        or list(vars(live)["_member_names_"]) != [key for key, _ in declared]
        or any(vars(live)[key] is not member or member._name_ != key for key, member in members)
        or any(
            vars(live)[key] != vars(reference)[key]
            for key in ("_hashable_values_", "_unhashable_values_")
        )
        or {value: id(vars(live)[key]) for key, value in declared}
        != {value: id(member) for value, member in vars(live)["_value2member_map_"].items()}
    ):
        raise ValueError("Live enum differs from source")
