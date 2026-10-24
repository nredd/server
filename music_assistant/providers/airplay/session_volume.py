"""
Default-off logical session volume and checked CLI actuator receipts.

References:
    - airplay-cli/build/session-volume-contract.md (frozen IPC v1)

Receiver telemetry is deliberately not an input. Binding requires an independently
verified native identity; discovering a stream never publishes native availability.
"""

from __future__ import annotations

import asyncio
import itertools
import re
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, cast

from music_assistant_models.constants import PLAYER_CONTROL_NATIVE, PLAYER_CONTROL_NONE
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.controllers.players.volume_execution import CheckedVolumeExecution

if TYPE_CHECKING:
    from music_assistant.models.player import Player
    from music_assistant.providers.airplay.player import AirPlayPlayer
    from music_assistant.providers.airplay.provider import AirPlayProvider
    from music_assistant.providers.airplay.sendspin_bridge import BridgeVolumeContext
    from music_assistant.providers.airplay.stream import AirPlayStream

_UINT64_MAX = (1 << 64) - 1
_GENERATIONS = itertools.count(1)
_SET_EVENT = re.compile(
    r"\[EVENT\] session_volume_set generation=([0-9]{1,20}) "
    r"ordinal=([0-9]{1,20}) milli=([0-9]{1,4})"
)
_RESULT_EVENT = re.compile(
    r"\[EVENT\] stream_volume_result generation=([0-9]{1,20}) "
    r"ordinal=([0-9]{1,20}) status=(committed|failed|unknown)(?: level=([0-9]{1,3}))?"
)
_DEADLINE = 2.5  # Leave time for the C driver's three-second completion deadline.


class VolumeResult(StrEnum):
    """Closed set of logical backend outcomes."""

    COMMITTED = "committed"
    REJECTED = "rejected"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class VolumeRequest:
    """A validated local request, not a native wire identity."""

    generation: int
    ordinal: int
    milli: int


@dataclass(frozen=True)
class VolumeReceipt:
    """A checked raw-device actuator response; unknown is distinct from zero."""

    result: VolumeResult
    level: int | None = None


@dataclass(frozen=True)
class OwnerSnapshot:
    """Exact owner path, membership, process and stream-session identities."""

    owner_id: str
    topology: tuple[
        tuple[
            str,
            int,
            str | None,
            str | None,
            str | None,
            tuple[str, ...],
            str,
            int,
            int,
            str | None,
            str | None,
        ],
        ...,
    ]
    actuators: tuple[tuple[str, SessionVolumeBackend, object, object], ...]
    actuator_generations: tuple[int, ...]
    bridges: tuple[BridgeVolumeContext, ...] = ()


@dataclass(frozen=True)
class VolumeBinding:
    """Private verified identity associated with one immutable ownership snapshot."""

    generation: int
    output_uid: bytes
    endpoint_uid: bytes
    snapshot: OwnerSnapshot


def parse_request(line: str) -> VolumeRequest | None:
    """Parse only the exact bounded v1 stdout event grammar."""
    if (match := _SET_EVENT.fullmatch(line)) is None:
        return None
    generation, ordinal, milli = (int(value) for value in match.groups())
    if not 0 < generation <= _UINT64_MAX or not 0 < ordinal <= _UINT64_MAX or milli > 1000:
        return None
    return VolumeRequest(generation, ordinal, milli)


def parse_receipt(line: str) -> tuple[int, int, VolumeReceipt] | None:
    """Reject missing, extra, duplicate and incompatible receipt fields."""
    if (match := _RESULT_EVENT.fullmatch(line)) is None:
        return None
    generation, ordinal = int(match[1]), int(match[2])
    result = VolumeResult(match[3])
    level = int(match[4]) if match[4] is not None else None
    if not 0 < generation <= _UINT64_MAX or not 0 < ordinal <= _UINT64_MAX:
        return None
    if (result == VolumeResult.COMMITTED) != (level is not None):
        return None
    if level is not None and level > 100:
        return None
    return generation, ordinal, VolumeReceipt(result, level)


class SessionVolumeBackend:
    """One stream's bounded logical backend and independent actuator transaction lane."""

    def __init__(self, stream: AirPlayStream) -> None:
        """Create an unreported, unbound backend with no authoritative state."""
        self.stream = stream
        self.generation = next(_GENERATIONS)
        self.supported = False
        self.confirmed_level: int | None = None
        self._receipt_snapshot: OwnerSnapshot | None = None
        self.binding: VolumeBinding | None = None
        self._dependents: set[SessionVolumeBackend] = set()
        self.revision = 0
        self._balance: dict[str, int] | None = None
        self._ordinal = 0
        self._last_request = 0
        self._transaction_token = object()
        self._pending_snapshot: OwnerSnapshot | None = None
        self._pending_token: object | None = None
        self._pending: tuple[int, asyncio.Future[VolumeReceipt]] | None = None
        self._actuator_lock = asyncio.Lock()
        self._runner: asyncio.Task[None] | None = None
        self._trailing: VolumeRequest | None = None
        self._feedback_task: asyncio.Task[None] | None = None
        self._feedback_dirty = False
        self._unsubscribe = stream.mass.players.subscribe_player_state_update(self._state_changed)

    @property
    def enabled(self) -> bool:
        """Return operational policy plus handshake, never UI readiness."""
        return (
            self.stream.session_volume_requested
            and self.supported
            and self.stream.running
            and not self.stream.superseded
            and self.stream.player.stream is self.stream
            and self.stream.session_volume is self
        )

    def handle_stdout(self, line: str) -> bool:
        """Consume private v1 stdout records without logging their input values."""
        if line.startswith("[STATUS] session_volume"):
            if line == "[STATUS] session_volume version=1" and self.stream.session_volume_requested:
                self.supported = True
            return True
        if line.startswith("[EVENT] stream_volume_result"):
            if self.enabled and (parsed := parse_receipt(line)) is not None:
                generation, ordinal, receipt = parsed
                if (
                    generation == self.generation
                    and self._pending is not None
                    and self._pending[0] == ordinal
                    and not self._pending[1].done()
                ):
                    self._pending[1].set_result(receipt)
            return True
        if line.startswith("[EVENT] session_volume_set"):
            if self.enabled and (request := parse_request(line)) is not None:
                self.request(request)
            return True
        return False

    async def bind(
        self, output_uid: bytes, endpoint_uid: bytes, *, verified_identity: bool = False
    ) -> bool:
        """Bind only an externally verified identity; no production caller supplies one yet."""
        if not self.enabled or not verified_identity:
            return False
        if not output_uid or len(output_uid) > 128 or len(endpoint_uid) > 128:
            raise ValueError("Invalid session volume identity length")
        if self.binding is not None:
            raise ValueError("Session volume is already bound")
        snapshot = self.snapshot()
        binding = VolumeBinding(self.generation, output_uid, endpoint_uid, snapshot)
        self.binding = binding
        committed = False
        try:
            if not await self.stream.send_cli_command(
                f"SESSION_VOLUME_GENERATION={binding.generation}\n"
                f"SESSION_VOLUME_OUTPUT_UID_HEX={output_uid.hex()}\n"
                f"SESSION_VOLUME_ENDPOINT_UID_HEX={endpoint_uid.hex()}\nACTION=SESSION_VOLUME_BIND"
            ):
                return False
            if self.binding is not binding:
                return False
            self._validate()
            for _, backend, _, _ in snapshot.actuators:
                backend._dependents.add(self)
            await self.publish_state()
            committed = self.binding is binding
            return committed
        finally:
            if not committed and self.binding is binding:
                # Delivery may have reached C before cancellation. Retire this
                # generation so a retry cannot inherit that uncertain binding.
                await self.invalidate()

    def snapshot(self) -> OwnerSnapshot:
        """Resolve current volume authority, rejecting missing and contradictory ownership."""
        stream = self.stream
        if not self.enabled or stream.player.stream is not stream or stream.superseded:
            raise PlayerCommandFailed("Session volume stream is no longer current")
        context = cast("AirPlayProvider", stream.prov).bridge_manager.get_volume_command_context(
            stream.player.player_id
        )
        session = stream.session
        if context is None and session is None:
            raise PlayerCommandFailed("No confirmed volume session owner")
        controller = stream.mass.players
        path = self._owner_path(context.player if context else None)
        owner = path[-1]
        members = self._volume_members(owner)
        actuators: list[tuple[str, SessionVolumeBackend, object, object]] = []
        bridges: list[BridgeVolumeContext] = []
        for member in members:
            member_path = self._owner_path(member)
            if member_path[-1] is not owner:
                raise PlayerCommandFailed("Contradictory session volume member owner")
            path.extend(member_path)
            target = member
            if member.state.volume_control != PLAYER_CONTROL_NATIVE:
                resolved = controller.get_player(member.state.volume_control)
                if resolved is None:
                    raise PlayerCommandFailed("Unconfirmed session volume actuator")
                target = resolved
            if context is not None:
                member_context = self._member_bridge(target, context, owner)
                bridges.append(member_context)
                path.extend(self._owner_path(member_context.player))
                target = member_context.bridge.airplay_player
            if target.provider.domain != "airplay":
                raise PlayerCommandFailed("Unconfirmed mixed session volume actuators")
            target_stream = cast("AirPlayPlayer", target).stream
            if (
                target_stream is None
                or target_stream.session is not session
                or not target_stream.session_volume.enabled
                or target_stream.superseded
                or target_stream._cli_proc is None
            ):
                raise PlayerCommandFailed("Session volume actuator is not current")
            actuators.append(
                (
                    member.player_id,
                    target_stream.session_volume,
                    target_stream._cli_proc,
                    context.source if context else cast("object", session),
                )
            )
            path.extend([member, target])
        if len(actuators) != len({item[1] for item in actuators}):
            raise PlayerCommandFailed("Aliased session volume actuators")
        if context is not None:
            if {item.client for item in bridges} != set(context.members):
                raise PlayerCommandFailed("Bridge session membership changed")
        elif session is not None and (
            len(actuators) != len(session.sync_clients)
            or {backend.stream.player for _, backend, _, _ in actuators}
            != set(session.sync_clients)
        ):
            raise PlayerCommandFailed("Native session membership changed")
        topology = tuple(
            sorted(
                {
                    (
                        player.player_id,
                        id(player),
                        player.protocol_parent_id,
                        player.state.active_group,
                        player.state.synced_to,
                        tuple(sorted(player.state.group_members)),
                        player.state.volume_control,
                        controller.scale_volume_to_device(player.player_id, 0),
                        controller.scale_volume_to_device(player.player_id, 100),
                        player.state.active_source,
                        player.active_output_protocol,
                    )
                    for player in path
                }
            )
        )
        ordered = tuple(sorted(actuators, key=lambda item: item[0]))
        return OwnerSnapshot(
            owner.player_id,
            topology,
            ordered,
            tuple(item[1].generation for item in ordered),
            tuple(sorted(bridges, key=lambda item: item.player.player_id)),
        )

    async def apply(
        self, level: int, validate: Callable[[], None] | None = None, *, update_player: bool = True
    ) -> int:
        """Set raw device percent and await the actual accepted CLI result."""
        if not self.enabled or not 0 <= level <= 100:
            raise PlayerCommandFailed("Checked stream volume is unavailable")
        generation = self.generation
        transaction_token = self._transaction_token
        snapshot = self._checked_snapshot()

        def check() -> None:
            """Keep ordinary checked commands bound to the same logical source too."""
            self._checked_snapshot(snapshot, validate)

        def transaction_current() -> bool:
            """Allow FIFO delivery only while this checked transaction remains current."""
            return self.generation == generation and self._transaction_token is transaction_token

        check()
        async with self._actuator_lock:
            if (
                not self.enabled
                or generation != self.generation
                or self.stream.player.stream is not self.stream
            ):
                raise PlayerCommandFailed("Checked stream volume owner changed")
            check()
            if update_player and self.stream.player.volume_muted:
                level = 0
            self._ordinal += 1
            ordinal = self._ordinal
            future: asyncio.Future[VolumeReceipt] = asyncio.get_running_loop().create_future()
            self._pending_snapshot = snapshot
            self._pending_token = transaction_token
            self._pending = ordinal, future
            accepted = False
            try:
                async with asyncio.timeout(_DEADLINE):
                    if not await self.stream.send_cli_command(
                        f"STREAM_VOLUME_GENERATION={generation}\nSTREAM_VOLUME_ORDINAL={ordinal}\n"
                        f"STREAM_VOLUME_LEVEL={level}\nACTION=STREAM_VOLUME_APPLY",
                        guard=transaction_current,
                    ):
                        raise PlayerCommandFailed("Checked stream volume delivery failed")
                    receipt = await future
                if (
                    not self.enabled
                    or self.generation != generation
                    or self.stream.player.stream is not self.stream
                    or receipt.result != VolumeResult.COMMITTED
                    or receipt.level is None
                ):
                    raise PlayerCommandFailed("Checked stream volume is uncertain")
                check()
                self.confirmed_level = receipt.level
                try:
                    self._receipt_snapshot = self.snapshot()
                except PlayerCommandFailed:
                    self._receipt_snapshot = None
                if update_player and not self.stream.player.volume_muted:
                    self.stream.player._attr_volume_level = receipt.level
                self.stream.player.update_state()
                for dependent in self._dependents:
                    if dependent._runner is None or dependent._runner.done():
                        dependent._queue_feedback()
                accepted = True
                return receipt.level
            finally:
                self._finish_transaction(generation, transaction_token, future, accepted)

    def request(self, request: VolumeRequest) -> None:
        """Admit at most one executing request and one replaceable trailing request."""
        if (
            self.binding is None
            or request.generation != self.binding.generation
            or not 0 < request.ordinal <= _UINT64_MAX
            or not 0 <= request.milli <= 1000
            or request.ordinal <= self._last_request
        ):
            return
        try:
            self._validate()
        except PlayerCommandFailed:
            return
        self._last_request = request.ordinal
        self._trailing = request
        if self._runner is None or self._runner.done():
            self._runner = self.stream.mass.create_task(self._run())

    async def publish_state(self) -> None:
        """Publish receipts only; feedback never invokes a volume command."""
        if self.binding is None or self.binding.generation != self.generation:
            return
        try:
            self._validate()
        except PlayerCommandFailed:
            await self.invalidate()
            return
        self.revision += 1
        await self.stream.send_cli_command(
            f"SESSION_VOLUME_GENERATION={self.binding.generation}\n"
            f"SESSION_VOLUME_REVISION={self.revision}\n{self._state_fields()}"
            "ACTION=SESSION_VOLUME_STATE"
        )

    async def invalidate(self) -> None:
        """Unbind, cancel local work and discard receipts on ownership or process loss."""
        binding = self._detach_binding()
        self._transaction_token = object()
        self._discard_receipt()
        self.generation = next(_GENERATIONS)
        self._trailing = None
        self._balance = None
        if self._runner is not None and self._runner is not asyncio.current_task():
            self._runner.cancel()
        if self._pending is not None:
            self._pending[1].cancel()
        if self._feedback_task is not None and self._feedback_task is not asyncio.current_task():
            self._feedback_task.cancel()
        if binding is not None:
            await self._send_unbind(binding)

    def owner_changed(self) -> None:
        """Retire pending work synchronously before a bridge source can be replaced."""
        binding = self._detach_binding()
        self._transaction_token = object()
        self._discard_receipt()
        self.generation = next(_GENERATIONS)
        self._balance = None
        self._trailing = None
        if self._pending is not None:
            self._pending[1].cancel()
        if self._runner is not None and self._runner is not asyncio.current_task():
            self._runner.cancel()
        if binding is not None:
            self.stream.mass.create_task(self._send_unbind(binding))

    async def close(self) -> None:
        """Detach the state subscription before stream teardown or reconnect."""
        self._unsubscribe()
        await self.invalidate()
        self.supported = False
        for task in (self._runner, self._feedback_task):
            if task is not None and task is not asyncio.current_task():
                with suppress(asyncio.CancelledError):
                    await task

    def _detach_binding(self) -> VolumeBinding | None:
        """Detach an installed binding and every dependent before its asynchronous cleanup."""
        binding, self.binding = self.binding, None
        if binding is not None:
            for _, backend, _, _ in binding.snapshot.actuators:
                backend._dependents.discard(self)
                backend._discard_receipt()
        return binding

    async def _send_unbind(self, binding: VolumeBinding) -> None:
        """Deliver a retired binding's captured-generation UNBIND without touching new state."""
        if (
            self.supported
            and self.stream.session_volume_requested
            and self.stream.running
            and self.stream.session_volume is self
        ):
            await self.stream.send_cli_command(
                f"SESSION_VOLUME_GENERATION={binding.generation}\nACTION=SESSION_VOLUME_UNBIND"
            )

    def _finish_transaction(
        self,
        generation: int,
        transaction_token: object,
        future: asyncio.Future[VolumeReceipt],
        accepted: bool,
    ) -> None:
        """Clear only the pending state that belongs to this completed transaction."""
        if not accepted and self.generation == generation:
            self._discard_receipt()
        if self._pending_token is transaction_token:
            self._pending = None
            self._pending_snapshot = None
            self._pending_token = None
        future.cancel()

    def _checked_snapshot(
        self, expected: OwnerSnapshot | None = None, validate: Callable[[], None] | None = None
    ) -> OwnerSnapshot:
        """Discard authority on failed validation even before actuator dispatch."""
        try:
            current = self.snapshot()
            if validate is not None:
                validate()
        except PlayerCommandFailed:
            self._discard_receipt()
            raise
        if expected is not None and expected != current:
            self._discard_receipt()
            raise PlayerCommandFailed("Checked volume source changed")
        return current

    def _volume_members(self, owner: Player) -> list[Player]:
        """Require all logical members, including those Core would filter as unavailable."""
        members = (
            list(
                self.stream.mass.players.iter_group_members(
                    owner, only_powered=True, exclude_self=False
                )
            )
            if owner.state.group_members
            else [owner]
        )
        if not members or any(
            member.state.volume_control == PLAYER_CONTROL_NONE
            or not member.state.available
            or not member.state.enabled
            or member.state.powered is False
            for member in members
        ):
            raise PlayerCommandFailed("Session volume has unavailable members")
        if owner.state.group_members and set(owner.state.group_members) | {owner.player_id} != {
            member.player_id for member in members
        } | {owner.player_id}:
            raise PlayerCommandFailed("Session volume membership is incomplete")
        return members

    def _member_bridge(
        self, target: Player, context: BridgeVolumeContext, owner: Player
    ) -> BridgeVolumeContext:
        """Require the exact SDK client for each logical actuator in the same source."""
        manager = cast("AirPlayProvider", self.stream.prov).bridge_manager
        for candidate in self.stream.prov.players:
            bridge = manager.get_bridge(candidate.player_id)
            if bridge is None or (
                bridge.bridge_client_id != target.player_id and bridge.airplay_player is not target
            ):
                continue
            member = manager.get_volume_command_context(candidate.player_id)
            if (
                member is not None
                and (member.player is target or member.bridge.airplay_player is target)
                and member.group is context.group
                and member.source is context.source
                and self._owner_path(member.player)[-1] is owner
            ):
                return member
            break
        raise PlayerCommandFailed("Unconfirmed mixed bridge volume actuators")

    def _owner_path(self, player: Player | None = None) -> list[Player]:
        """Follow explicit volume-owner edges without transport routing heuristics."""
        current: Player = player or self.stream.player
        seen: set[str] = set()
        path: list[Player] = []
        while True:
            if self.stream.mass.players.get_player(current.player_id) is not current:
                raise PlayerCommandFailed("Session volume owner is no longer registered")
            if current.player_id in seen:
                raise PlayerCommandFailed("Cyclic session volume ownership")
            seen.add(current.player_id)
            path.append(current)
            state = current.state
            if state.active_group and state.synced_to and state.active_group != state.synced_to:
                for ancestor in self._sync_ancestors(current):
                    if ancestor.state.active_group != state.active_group:
                        raise PlayerCommandFailed("Contradictory session volume ownership")
                    path.append(ancestor)
                    seen.add(ancestor.player_id)
            next_id = current.protocol_parent_id or state.active_group or state.synced_to
            if not next_id:
                return path
            parent = self.stream.mass.players.get_player(next_id)
            if parent is None:
                raise PlayerCommandFailed("Missing session volume owner")
            if (
                next_id != current.protocol_parent_id
                and current.player_id not in parent.state.group_members
            ):
                raise PlayerCommandFailed("Session volume owner excludes originating member")
            current = parent

    def _sync_ancestors(self, player: Player) -> list[Player]:
        """Check a sync chain converges inside the same explicit active group."""
        ancestors: list[Player] = []
        seen = {player.player_id}
        current = player
        while current.state.synced_to:
            leader = self.stream.mass.players.get_player(current.state.synced_to)
            if leader is None or leader.player_id in seen:
                raise PlayerCommandFailed("Missing or cyclic session volume sync owner")
            if current.player_id not in leader.state.group_members:
                raise PlayerCommandFailed("Contradictory session volume sync membership")
            seen.add(leader.player_id)
            ancestors.append(leader)
            current = leader
        return ancestors

    def _validate(self) -> None:
        """Recheck the complete snapshot across every asynchronous execution boundary."""
        if self.binding is None or self.binding.snapshot != self.snapshot():
            raise PlayerCommandFailed("Session volume binding is stale")

    def _levels(self) -> dict[str, int] | None:
        """Return logical member receipts only when all current members are confirmed."""
        if self.binding is None:
            return None
        levels: dict[str, int] = {}
        for member_id, backend, _, _ in self.binding.snapshot.actuators:
            if backend.confirmed_level is None or backend._receipt_snapshot is None:
                return None
            try:
                if backend._receipt_snapshot != backend.snapshot():
                    return None
            except PlayerCommandFailed:
                return None
            levels[member_id] = max(
                0,
                min(
                    100,
                    self.stream.mass.players.scale_volume_from_device(
                        member_id, backend.confirmed_level
                    ),
                ),
            )
        return levels or None

    def _state_fields(self) -> str:
        """Serialize explicit unknown separately from confirmed zero."""
        if (levels := self._levels()) is None:
            return "SESSION_VOLUME_KNOWN=0\n"
        return f"SESSION_VOLUME_KNOWN=1\nSESSION_VOLUME_MILLI={max(levels.values()) * 10}\n"

    async def _run(self) -> None:
        """Drain the bounded trailing slot without optimistic success or rollback."""
        while (request := self._trailing) is not None:
            self._trailing = None
            result = VolumeResult.UNKNOWN
            try:
                self._validate()
                levels = self._levels()
                if levels is None:
                    result = VolumeResult.REJECTED
                else:
                    if self._balance is None:
                        self._balance = dict(levels)
                    async with asyncio.timeout(_DEADLINE):
                        await self.stream.mass.players.checked_group_volume(
                            self.binding.snapshot.owner_id if self.binding else "",
                            (request.milli + 5) // 10,
                            CheckedVolumeExecution(
                                self._validate,
                                self._preflight,
                                self._apply_member,
                                levels,
                                self._balance,
                            ),
                        )
                    self._validate()
                    result = VolumeResult.COMMITTED
            except asyncio.CancelledError:
                if self.binding is not None:
                    for _, backend, _, _ in self.binding.snapshot.actuators:
                        backend._discard_receipt()
                raise
            except PlayerCommandFailed, TimeoutError:
                # Some members may have accepted the operation. Clear every receipt;
                # a later explicit checked commit can reconcile, never a rollback.
                if self.binding is not None:
                    for _, backend, _, _ in self.binding.snapshot.actuators:
                        backend._discard_receipt()
            if self.binding is None or request.generation != self.binding.generation:
                continue
            try:
                self._validate()
            except PlayerCommandFailed:
                await self.invalidate()
                return
            self.revision += 1
            await self.stream.send_cli_command(
                f"SESSION_VOLUME_GENERATION={request.generation}\n"
                f"SESSION_VOLUME_ORDINAL={request.ordinal}\nSESSION_VOLUME_RESULT={result}\n"
                f"SESSION_VOLUME_REVISION={self.revision}\n{self._state_fields()}"
                "ACTION=SESSION_VOLUME_COMPLETE"
            )

    def _preflight(self, member_id: str) -> None:
        """Check all final actuators before dispatch and again under each member lock."""
        self._validate()
        if (
            self.binding is None
            or self._levels() is None
            or member_id not in {item[0] for item in self.binding.snapshot.actuators}
        ):
            raise PlayerCommandFailed("Unconfirmed session volume member")

    async def _apply_member(self, member_id: str, level: int) -> int:
        """Apply an already-scaled target directly, without protocol-parent rescaling."""
        self._preflight(member_id)
        assert self.binding is not None
        for candidate, backend, _, _ in self.binding.snapshot.actuators:
            if candidate == member_id:
                bridge = next(
                    (
                        item.bridge
                        for item in self.binding.snapshot.bridges
                        if item.stream.session_volume is backend
                    ),
                    None,
                )
                committed = (
                    await bridge.apply_volume_confirmed(level, lambda: self._preflight(member_id))
                    if bridge is not None
                    else await backend.apply(level, lambda: self._preflight(member_id))
                )
                self._validate()
                return committed
        raise PlayerCommandFailed("Missing session volume actuator")

    def _discard_receipt(self) -> None:
        """Discard authority and queue feedback for every binding using this actuator."""
        self.confirmed_level = None
        self._receipt_snapshot = None
        candidates = self._dependents | {self}
        for backend in candidates:
            if backend.binding is not None and any(
                actuator is self for _, actuator, _, _ in backend.binding.snapshot.actuators
            ):
                backend._queue_feedback()

    def _queue_feedback(self) -> None:
        """Coalesce feedback without losing receipt changes during an in-flight write."""
        if self.binding is None or self.binding.generation != self.generation:
            return
        self._feedback_dirty = True
        if self._feedback_task is None or self._feedback_task.done():
            self._feedback_task = self.stream.mass.create_task(self._publish_feedback(self.binding))

    async def _publish_feedback(self, binding: VolumeBinding) -> None:
        """Drain state-only updates for the exact binding that scheduled this task."""
        while (
            self._feedback_dirty
            and self.binding is binding
            and binding.generation == self.generation
        ):
            self._feedback_dirty = False
            await self.publish_state()

    def _state_changed(self, player: Player, changes: dict[str, tuple[object, object]]) -> None:
        """Invalidate topology changes; receiver volume reports never initialize state."""
        snapshots = tuple(
            snapshot
            for snapshot in (
                (
                    self.binding.snapshot
                    if self.binding is not None and self.binding.generation == self.generation
                    else None
                ),
                self._receipt_snapshot,
                (
                    self._pending_snapshot
                    if self._pending_token is self._transaction_token
                    else None
                ),
            )
            if snapshot is not None
        )
        if not snapshots or not any(
            player.player_id in {item[0] for item in snapshot.topology} for snapshot in snapshots
        ):
            return
        try:
            current = self.snapshot()
        except PlayerCommandFailed:
            current = None
        if any(snapshot != current for snapshot in snapshots):
            self.owner_changed()
        if self.binding is None:
            return
        if changes.keys() & {"volume_level", "volume_control", "volume_muted"} and (
            self._runner is None or self._runner.done()
        ):
            self._balance = None
        self._queue_feedback()
