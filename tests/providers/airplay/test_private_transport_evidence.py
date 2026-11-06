"""
Independent substitution controls for the fourteen-source producer.

References:
    https://docs.python.org/3/library/types.html#types.FunctionType
"""

from __future__ import annotations

import sys
from types import FunctionType, ModuleType
from typing import Any

import pytest

from music_assistant.controllers.player_queues import helpers as queue_helpers
from music_assistant.controllers.player_queues.controller import PlayerQueuesController
from music_assistant.controllers.players.controller import PlayerController
from music_assistant.helpers import private_transport
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
