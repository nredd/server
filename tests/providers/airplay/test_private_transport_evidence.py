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
