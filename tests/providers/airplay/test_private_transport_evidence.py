"""
Independent substitution controls for the fourteen-source producer.

References:
    https://docs.python.org/3/library/types.html#types.FunctionType
"""

from __future__ import annotations

import ast
import builtins
import dis
import functools
import re
import sys
from pathlib import Path
from types import FunctionType, ModuleType
from typing import Any

import pytest

from music_assistant.controllers.player_queues import helpers as queue_helpers
from music_assistant.controllers.player_queues.controller import PlayerQueuesController
from music_assistant.controllers.players.controller import PlayerController
from music_assistant.helpers import private_transport, private_trial
from music_assistant.helpers import process as process_module
from music_assistant.providers.airplay import (
    player,
    provider,
    server_code_evidence,
    stream,
    stream_session,
    trial_binary,
)

BOUNDARIES = [
    (private_transport, "validate_request"),
    *[
        (private_transport.PrivateTransportAttempt, name)
        for name in (
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
            "_play",
            "_watchdog",
            "_cleanup",
            "_release",
            "_read_pipe",
            "_drain",
        )
    ],
    (provider.AirPlayProvider, "private_trial_transport"),
    (provider.AirPlayProvider, "_private_transport_current"),
    *[
        (stream_session.AirPlayStreamSession, name)
        for name in (
            "start",
            "private_release",
            "_start_client",
            "_start_player_ffmpeg",
            "_write_chunk_to_all_players",
        )
    ],
    *[
        (stream.AirPlayStream, name)
        for name in (
            "connect",
            "start",
            "private_release",
            "_write_cli_command",
            "_connect_volume_allowed",
            "_create_private_pipe",
        )
    ],
]


def evidence() -> dict[str, Any]:
    """Run the real producer against a real uninitialized controller."""
    return server_code_evidence.server_code_evidence(
        provider, PlayerController.__new__(PlayerController)
    )


def test_exact_fourteen_source_inventory() -> None:
    """The public producer has a closed fourteen-source contract."""
    result = evidence()
    assert result["status"] == "verified"
    assert set(result["source_hashes"]) == {
        "provider",
        "control_player",
        "player",
        "helpers",
        "constants",
        "trial_binary",
        "private_trial",
        "server_code_evidence",
        "players_controller",
        "sendspin_bridge",
        "private_transport",
        "queue_helpers",
        "stream_session",
        "stream",
    }


@pytest.mark.parametrize(("owner", "name"), BOUNDARIES)
def test_each_boundary_rejects_foreign_globals(
    monkeypatch: pytest.MonkeyPatch,
    owner: Any,
    name: str,
) -> None:
    """Matching code/defaults are insufficient when the callable uses foreign globals."""
    function = getattr(owner, name)
    clone = FunctionType(
        function.__code__,
        dict(function.__globals__),
        name=function.__name__,
        argdefs=function.__defaults__,
        closure=function.__closure__,
    )
    clone.__kwdefaults__ = function.__kwdefaults__
    monkeypatch.setattr(owner, name, clone)
    assert evidence()["status"] == "unverified"


@pytest.mark.parametrize("mutation", ["wrapper", "wrapped", "defaults", "context", "class"])
def test_queue_wrapper_and_runtime_bindings_are_independent_controls(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    """Reject wrapper/closure substitutions and equal but wrongly typed defaults."""
    wrapper: Any = PlayerQueuesController.play_index
    if mutation == "wrapper":
        clone = FunctionType(
            wrapper.__code__, dict(wrapper.__globals__), closure=wrapper.__closure__
        )
        clone.__dict__["__wrapped__"] = wrapper.__wrapped__
        monkeypatch.setattr(PlayerQueuesController, "play_index", clone)
    elif mutation == "wrapped":
        monkeypatch.setattr(wrapper, "__wrapped__", lambda: None)
    elif mutation == "defaults":
        monkeypatch.setattr(wrapper.__wrapped__, "__defaults__", (0, 0))
    elif mutation == "context":
        monkeypatch.setattr(stream_session, "CURRENT_ATTEMPT", object())
    else:
        monkeypatch.setattr(provider, "PrivateTransportAttempt", object)
    assert evidence()["status"] == "unverified"


@pytest.mark.parametrize(
    ("owner", "name"),
    [
        (provider, "QueueOption"),
        (provider, "PrivateTransportAttempt"),
        (provider, "validate_request"),
        (player, "AirPlayStreamSession"),
        (stream_session, "AirPlayStream"),
        (stream_session, "FFMpeg"),
        *[
            (module, "CURRENT_ATTEMPT")
            for module in (queue_helpers, player, stream_session, stream)
        ],
    ],
)
def test_transport_dependency_substitution_is_rejected(
    monkeypatch: pytest.MonkeyPatch, owner: ModuleType, name: str
) -> None:
    """Reject each transport dependency and shared context binding independently."""
    monkeypatch.setattr(owner, name, object())
    assert evidence()["status"] == "unverified"


def test_wrapped_callable_cannot_select_its_own_trust_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even matching source and closure cannot nominate foreign module globals."""
    wrapper: Any = PlayerQueuesController.play_index
    original = wrapper.__wrapped__
    module = ModuleType("private_transport_forged_queue")
    vars(module).update(original.__globals__)
    module.__name__ = "private_transport_forged_queue"
    wrapped = FunctionType(original.__code__, vars(module), argdefs=original.__defaults__)
    wrapped.__kwdefaults__ = original.__kwdefaults__

    def capture() -> Any:
        """Produce the exact one-cell closure required by functools.wraps."""
        return wrapped

    clone = FunctionType(wrapper.__code__, wrapper.__globals__, closure=capture.__closure__)
    clone.__dict__["__wrapped__"] = wrapped
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(PlayerQueuesController, "play_index", clone)
    assert evidence()["status"] == "unverified"


def test_start_keyword_default_type_is_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    """A boolean keyword default cannot be silently replaced by equal integer zero."""
    monkeypatch.setattr(stream.AirPlayStream.start, "__kwdefaults__", {"join": 0})
    assert evidence()["status"] == "unverified"


PROCESS_METHODS = (
    "__init__",
    "start",
    "write",
    "read",
    "readexactly",
    "communicate",
    "verified_stdin_fd",
    "enter_receipt_mode",
    "read_receipt_chunk",
    "discard_receipt_stream",
    "reject_receipt_stream",
    "read_stdout",
    "close",
    "kill",
    "has_historical_receipt",
    "has_current_receipt",
    "_verify_acknowledgement",
    "_read_attestation_line",
    "_current_receipt",
    "_kill_and_reap",
    "_release_after_exit",
    "_release_asset_fd",
    "_release_receipt_owner",
    "_receipt_read",
    "_screen_receipt_bytes",
)
TRUST_FUNCTIONS = (
    "_parse_attestation_line",
    "_reject_duplicate_json_keys",
    "create_private_process",
    "_open_private_asset_fd",
)


@pytest.mark.parametrize("name", PROCESS_METHODS)
def test_trial_process_method_rejects_foreign_globals(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Every trust-bearing wrapper method is sealed against a globals-substituted clone."""
    function = getattr(trial_binary.TrialAsyncProcess, name)
    clone = FunctionType(
        function.__code__,
        dict(function.__globals__),
        name=function.__name__,
        argdefs=function.__defaults__,
        closure=function.__closure__,
    )
    clone.__kwdefaults__ = function.__kwdefaults__
    monkeypatch.setattr(trial_binary.TrialAsyncProcess, name, clone)
    assert evidence()["status"] == "unverified"


@pytest.mark.parametrize("name", TRUST_FUNCTIONS)
def test_trial_module_function_rejects_foreign_globals(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Module-level parser and factory code cannot be swapped for a globals clone."""
    function = getattr(trial_binary, name)
    clone = FunctionType(
        function.__code__,
        dict(function.__globals__),
        name=function.__name__,
        argdefs=function.__defaults__,
        closure=function.__closure__,
    )
    clone.__kwdefaults__ = function.__kwdefaults__
    monkeypatch.setattr(trial_binary, name, clone)
    assert evidence()["status"] == "unverified"


@pytest.mark.parametrize("name", ["receipt_rejected", "receipt_eof"])
def test_receipt_property_getter_alias_and_setter_are_rejected(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """A getter replaced by an alias, or given a setter, no longer matches its source."""
    cls = trial_binary.TrialAsyncProcess
    original: property = vars(cls)[name]
    monkeypatch.setattr(cls, name, property(lambda _self: True))
    assert evidence()["status"] == "unverified"
    monkeypatch.setattr(cls, name, original.setter(lambda _self, _value: None))
    assert evidence()["status"] == "unverified"


def test_factory_subclass_and_extra_members_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subclass bound for the factory, or any added class member, is refused."""

    class Forged(trial_binary.TrialAsyncProcess):
        """Override the trust predicate."""

        def has_current_receipt(self) -> bool:
            """Claim a current receipt."""
            return True

    monkeypatch.setattr(trial_binary, "TrialAsyncProcess", Forged)
    assert evidence()["status"] == "unverified"
    monkeypatch.undo()
    monkeypatch.setattr(trial_binary.TrialAsyncProcess, "trust", lambda *_a: None, raising=False)
    assert evidence()["status"] == "unverified"
    monkeypatch.undo()
    assert evidence()["status"] == "verified"


@pytest.mark.parametrize("name", ["cast", "suppress"])
def test_owner_handoff_dependencies_are_bound(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """A substituted `cast`/`suppress` could forge the discard decision; both are sealed."""
    monkeypatch.setattr(private_transport, name, lambda *_a, **_k: None)
    assert evidence()["status"] == "unverified"


TRIAL_CONSTANTS: dict[str, Any] = {
    "EXPECTED_SHA": "0" * 64,
    "EXPECTED_BUILD": "v0000000",
    "TRIAL_ROOT": Path("/forged-root"),
    "ASSET_NAME": "forged-asset",
    "MAX_BINARY_SIZE": 1,
    "MAX_CHECK_OUTPUT": 1,
    "CHECK_TIMEOUT": 99.0,
    "ATTEST_TIMEOUT": 99,
    "MAX_ATTEST_LINE": 1,
    "MAX_RECEIPT_CHUNK": 1,
    "_ATTEST_PREFIX": b"[FORGED]",
    "PROC_ROOT": Path("/forged-proc"),
    "_BUILD_LABEL": re.compile("forged"),
}
TRIAL_BINDINGS = (
    "asyncio",
    "errno",
    "hashlib",
    "json",
    "os",
    "re",
    "secrets",
    "stat",
    "suppress",
    "dataclass",
    "Path",
    "AsyncProcess",
    "descriptor_path",
    "get_subprocess_env",
    "open_in_thread",
    "encode_public_digest",
    "_is_trial_target",
    "TrialBinaryError",
    "ChildAttestationChangedError",
    "_TrustedChildReceipt",
)
TRIAL_FUNCTIONS = sorted(
    name
    for name, value in vars(trial_binary).items()
    if isinstance(value, FunctionType) and value.__module__ == trial_binary.__name__
)
PROCESS_HELPERS = (
    "get_subprocess_env",
    "descriptor_path",
    "open_in_thread",
    "_close_late_descriptor",
)


@pytest.mark.parametrize("name", sorted(TRIAL_CONSTANTS))
def test_trial_qualification_constant_substitution_is_rejected(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Every pin, root, limit and pattern the qualification reads is sealed to its source."""
    monkeypatch.setattr(trial_binary, name, TRIAL_CONSTANTS[name])
    assert evidence()["status"] == "unverified"


@pytest.mark.parametrize("name", TRIAL_BINDINGS)
def test_trial_qualification_binding_substitution_is_rejected(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Every imported alias and local class the qualification reads is the canonical object."""
    monkeypatch.setattr(trial_binary, name, object())
    assert evidence()["status"] == "unverified"


@pytest.mark.parametrize("name", TRIAL_FUNCTIONS)
def test_every_trial_binary_function_rejects_foreign_globals(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """The whole qualification closure is sealed, not just the receipt entry points."""
    function = getattr(trial_binary, name)
    clone = FunctionType(
        function.__code__,
        dict(function.__globals__),
        name=function.__name__,
        argdefs=function.__defaults__,
        closure=function.__closure__,
    )
    clone.__kwdefaults__ = function.__kwdefaults__
    monkeypatch.setattr(trial_binary, name, clone)
    assert evidence()["status"] == "unverified"


@pytest.mark.parametrize("name", PROCESS_HELPERS)
def test_process_helper_closure_is_sealed(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """The helpers the qualification delegates to match a fresh compile of their source."""
    function = getattr(process_module, name)
    clone = FunctionType(
        function.__code__,
        dict(function.__globals__),
        name=function.__name__,
        argdefs=function.__defaults__,
        closure=function.__closure__,
    )
    clone.__kwdefaults__ = function.__kwdefaults__
    monkeypatch.setattr(process_module, name, clone)
    assert evidence()["status"] == "unverified"
    monkeypatch.undo()
    monkeypatch.setattr(process_module, name, lambda *_a, **_k: None)
    assert evidence()["status"] == "unverified"


def test_pinned_fixture_style_patch_cannot_keep_evidence_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact bypass: a forged pin plus a substituted descriptor path or asset opener."""
    monkeypatch.setattr(trial_binary, "EXPECTED_SHA", "0" * 64)
    monkeypatch.setattr(trial_binary, "descriptor_path", lambda _fd: "/bin/sh")
    assert evidence()["status"] == "unverified"
    monkeypatch.undo()
    monkeypatch.setattr(trial_binary, "_open_validated_asset", lambda: (0, "0" * 64))
    assert evidence()["status"] == "unverified"


def test_injected_trial_binary_function_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A function added to the module is outside its source closure."""
    monkeypatch.setattr(trial_binary, "_forged", lambda: None, raising=False)
    assert evidence()["status"] == "unverified"


def _global_names(function: FunctionType) -> set[str]:
    """Return every non-builtin global a function and its nested code reads."""
    names: set[str] = set()
    stack = [function.__code__]
    while stack:
        code = stack.pop()
        stack.extend(item for item in code.co_consts if hasattr(item, "co_code"))
        names |= {ins.argval for ins in dis.get_instructions(code) if ins.opname == "LOAD_GLOBAL"}
    return {name for name in names if name not in vars(builtins)}


def test_qualification_closure_reads_only_declared_globals() -> None:
    """Audit: nothing reachable from the factory/attestation path reads an unsealed global."""
    entries: list[FunctionType] = [
        value
        for value in vars(trial_binary).values()
        if isinstance(value, FunctionType) and value.__module__ == trial_binary.__name__
    ]
    for cls in (trial_binary.TrialAsyncProcess, trial_binary._TrustedChildReceipt):
        for value in vars(cls).values():
            function = value.fget if isinstance(value, property) else value
            # Skip dataclass-generated methods (compiled from "<string>").
            if (
                isinstance(function, FunctionType)
                and function.__code__.co_filename == trial_binary.__file__
            ):
                entries.append(function)
    reached = set().union(*(_global_names(function) for function in entries))
    assert reached <= server_code_evidence.TRIAL_GLOBALS
    for name, allowed in server_code_evidence.PROCESS_HELPER_GLOBALS.items():
        assert _global_names(getattr(process_module, name)) == allowed
    for name, allowed in server_code_evidence.PRIVATE_TRIAL_HELPER_GLOBALS.items():
        assert _global_names(getattr(private_trial, name)) == allowed
    # Helpers reached by name must themselves be inventoried.
    assert {"descriptor_path", "get_subprocess_env", "open_in_thread"} <= reached
    assert "encode_public_digest" in reached


SHADOWED_BUILTINS = ("bytes", "len", "isinstance", "bytearray", "type")
SHADOW_MODULES = {
    "trial_binary": trial_binary,
    "private_transport": private_transport,
    "private_trial": private_trial,
    "process_module": process_module,
    "stream": stream,
    "stream_session": stream_session,
    "provider": provider,
    "server_code_evidence": server_code_evidence,
}


class _Shadow:
    """A class standing in for a builtin."""

    def method(self, *_args: object) -> int:
        """Bound-method shadow target."""
        return 0


def _shadow(kind: str, name: str) -> object:
    builtin = getattr(builtins, name)
    if kind == "partial":
        return functools.partial(builtin)
    if kind == "class":
        return _Shadow
    if kind == "bound_method":
        return _Shadow().method
    if kind == "function":
        return lambda *_a, **_k: builtin(*_a, **_k)
    return object()


@pytest.mark.parametrize("kind", ["partial", "class", "bound_method", "function", "object"])
@pytest.mark.parametrize("name", SHADOWED_BUILTINS)
@pytest.mark.parametrize("module_name", sorted(SHADOW_MODULES))
def test_undeclared_builtin_shadow_in_sealed_module_is_rejected(
    monkeypatch: pytest.MonkeyPatch, module_name: str, name: str, kind: str
) -> None:
    """A module attribute of ANY type shadows a builtin read by sealed code; reject it."""
    monkeypatch.setattr(SHADOW_MODULES[module_name], name, _shadow(kind, name), raising=False)
    assert evidence()["status"] == "unverified"


@pytest.mark.parametrize("module_name", sorted(SHADOW_MODULES))
def test_undeclared_nonbuiltin_global_in_sealed_module_is_rejected(
    monkeypatch: pytest.MonkeyPatch, module_name: str
) -> None:
    """The inventory is closed against the module's own source, not only builtin names."""
    monkeypatch.setattr(SHADOW_MODULES[module_name], "_forged_global", 1, raising=False)
    assert evidence()["status"] == "unverified"


@pytest.mark.parametrize("module_name", sorted(SHADOW_MODULES))
def test_interpreter_warnings_registry_dict_is_accepted_other_types_rejected(
    monkeypatch: pytest.MonkeyPatch, module_name: str
) -> None:
    """The interpreter adds a dict `__warningregistry__` once a module emits a warning."""
    module = SHADOW_MODULES[module_name]
    monkeypatch.setattr(module, "__warningregistry__", {}, raising=False)
    assert evidence()["status"] == "verified"
    monkeypatch.setattr(module, "__warningregistry__", print, raising=False)
    assert evidence()["status"] == "unverified"


@pytest.mark.parametrize("module_name", sorted(SHADOW_MODULES))
def test_swapped_module_builtins_mapping_is_rejected(
    monkeypatch: pytest.MonkeyPatch, module_name: str
) -> None:
    """A per-module `__builtins__` mapping is a builtin shadow too."""
    module = SHADOW_MODULES[module_name]
    monkeypatch.setattr(
        module, "__builtins__", {**vars(builtins), "bytes": functools.partial(bytes)}
    )
    assert evidence()["status"] == "unverified"


def test_declared_module_globals_are_the_live_inventory() -> None:
    """Control: the real namespaces are closed and fully verified (no false positives)."""
    for module in SHADOW_MODULES.values():
        assert not (
            set(vars(module))
            - server_code_evidence._declared_names(
                ast.parse(Path(str(module.__file__)).read_bytes())
            )
            - server_code_evidence._MODULE_DUNDERS
        )
    assert evidence()["status"] == "verified"


ASYNC_METHODS = sorted(server_code_evidence.ASYNC_PROCESS_METHODS)
ASYNC_PROPERTIES = sorted(server_code_evidence.ASYNC_PROCESS_PROPERTIES)


@pytest.mark.parametrize("name", ASYNC_METHODS)
def test_inherited_async_process_method_substitution_is_rejected(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """The exec/close path runs inherited `AsyncProcess` code; it is sealed to its source."""
    monkeypatch.setattr(process_module.AsyncProcess, name, lambda *_a, **_k: None)
    assert evidence()["status"] == "unverified"


@pytest.mark.parametrize("name", ASYNC_METHODS)
def test_inherited_async_process_foreign_globals_are_rejected(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """A same-code clone bound to another globals dictionary is refused."""
    function = vars(process_module.AsyncProcess)[name]
    clone = FunctionType(
        function.__code__,
        dict(function.__globals__),
        name=function.__name__,
        argdefs=function.__defaults__,
        closure=function.__closure__,
    )
    clone.__kwdefaults__ = function.__kwdefaults__
    monkeypatch.setattr(process_module.AsyncProcess, name, clone)
    assert evidence()["status"] == "unverified"


@pytest.mark.parametrize("name", ASYNC_PROPERTIES)
def test_inherited_async_process_property_substitution_is_rejected(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """`closed` and `returncode` feed the receipt and attestation checks."""
    monkeypatch.setattr(process_module.AsyncProcess, name, property(lambda _self: None))
    assert evidence()["status"] == "unverified"


@pytest.mark.parametrize("name", ["_forged", "__getattr__", "__getattribute__"])
def test_extra_async_process_member_is_rejected(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """The class dictionary is closed against its source body."""
    monkeypatch.setattr(process_module.AsyncProcess, name, lambda *_a: None, raising=False)
    assert evidence()["status"] == "unverified"


@pytest.mark.parametrize("name", ["_stdin_feeder_task", "_stderr_reader_task"])
def test_async_process_class_attribute_substitution_is_rejected(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Inherited class-level defaults the wrapper reads stay `None`."""
    monkeypatch.setattr(process_module.AsyncProcess, name, object())
    assert evidence()["status"] == "unverified"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PIPE_DRAIN_TIMEOUT", 10_000),
        ("STREAM_RELEASE_TIMEOUT", 10_000),
        ("SIGINT", 0),
        ("VERBOSE_LOG_LEVEL", 99),
        ("LOGGER", object()),
        ("logging", object()),
        ("Path", object()),
        ("suppress", object()),
    ],
)
def test_async_process_module_binding_substitution_is_rejected(
    monkeypatch: pytest.MonkeyPatch, name: str, value: object
) -> None:
    """Constants and bindings the inherited code reads are sealed."""
    monkeypatch.setattr(process_module, name, value)
    assert evidence()["status"] == "unverified"


def test_async_process_mro_is_closed() -> None:
    """A mixin spliced into the hierarchy is refused."""

    class Mixin:
        """Foreign base."""

    child = trial_binary.TrialAsyncProcess
    original = child.__bases__
    child.__bases__ = (Mixin, process_module.AsyncProcess)
    try:
        assert evidence()["status"] == "unverified"
    finally:
        child.__bases__ = original
    assert child.__bases__ == original
    assert evidence()["status"] == "verified"


def test_async_process_inventory_covers_every_inherited_dependency() -> None:
    """Audit: every AsyncProcess member reachable from the wrapper (self./super()) is sealed."""
    base = vars(process_module.AsyncProcess)

    def function(value: object) -> FunctionType | None:
        target = value.fget if isinstance(value, property) else value
        return target if isinstance(target, FunctionType) else None

    def attributes(target: FunctionType) -> tuple[set[str], set[str]]:
        names: set[str] = set()
        read: set[str] = set()
        stack = [target.__code__]
        while stack:
            code = stack.pop()
            stack.extend(item for item in code.co_consts if hasattr(item, "co_code"))
            for ins in dis.get_instructions(code):
                if ins.opname in {"LOAD_ATTR", "LOAD_SUPER_ATTR", "STORE_ATTR"}:
                    names.add(str(ins.argval))
                if ins.opname == "LOAD_GLOBAL":
                    read.add(str(ins.argval))
        return names, read

    pending = [
        name
        for value in vars(trial_binary.TrialAsyncProcess).values()
        if (target := function(value)) is not None
        for name in attributes(target)[0]
        if name in base
    ]
    reached: set[str] = set()
    global_reads: set[str] = set()
    while pending:
        name = pending.pop()
        if name in reached:
            continue
        reached.add(name)
        if (target := function(base[name])) is not None:
            names, read = attributes(target)
            global_reads |= read
            pending.extend(item for item in names if item in base)
    sealed = (
        server_code_evidence.ASYNC_PROCESS_METHODS
        | server_code_evidence.ASYNC_PROCESS_PROPERTIES
        | server_code_evidence.ASYNC_PROCESS_CLASS_ATTRS
    )
    assert reached <= sealed
    assert {name for name in global_reads if name not in vars(builtins)} <= (
        server_code_evidence.ASYNC_PROCESS_GLOBALS
    )
