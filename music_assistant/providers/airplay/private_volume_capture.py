"""
Private aggregate native-volume observation, NEVER upstream.

References:
    /Users/redd/code/airplay-cli-native-trial/PRIVATE_NATIVE_VOLUME_CAPTURE.md
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import count
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from music_assistant.helpers.json import SerializableType
    from music_assistant.helpers.process import AsyncProcess

_UINT64_MAX: Final = (1 << 64) - 1
_GENERATIONS = count(1)
_STATUS_PREFIX: Final = "[STATUS] native_volume_capture "
_UINT_RE: Final = re.compile(r"(?:0|[1-9][0-9]{0,19})")
_FIELDS: Final = [
    "version",
    "records",
    "overflow",
    "event_in",
    "event_out",
    "datastream_in",
    "datastream_out",
    "volume_get",
    "volume_result",
    "volume_set_candidate",
    "volume_feedback",
    "volume_availability",
    "volume_capabilities",
    "transport",
    "other",
    "parser_valid",
    "parser_invalid",
    "parser_unknown",
    "event_at_cutoff",
    "datastream_at_cutoff",
    "dacp",
    "subscription",
    "publication",
    "capability",
    "generation_at_cutoff",
    "binding_at_cutoff",
    "sender_candidate",
    "endpoint_sender_match",
    "output_sender_match",
    "session_candidate",
    "endpoint_session_match",
    "output_session_match",
    "group_candidate",
    "endpoint_group_match",
    "output_group_match",
]
_UINT_FIELDS: Final = frozenset(
    {
        "records",
        "event_in",
        "event_out",
        "datastream_in",
        "datastream_out",
        "volume_get",
        "volume_result",
        "volume_set_candidate",
        "volume_feedback",
        "volume_availability",
        "volume_capabilities",
        "transport",
        "other",
        "parser_valid",
        "parser_invalid",
        "parser_unknown",
        "generation_at_cutoff",
    }
)
_BOOL_FIELDS: Final = frozenset(
    {
        "overflow",
        "subscription",
        "publication",
        "capability",
        "binding_at_cutoff",
        "sender_candidate",
        "endpoint_sender_match",
        "output_sender_match",
        "session_candidate",
        "endpoint_session_match",
        "output_session_match",
        "group_candidate",
        "endpoint_group_match",
        "output_group_match",
    }
)
_PLANE_FIELDS: Final = frozenset({"event_at_cutoff", "datastream_at_cutoff"})
_DACP_CLASSES: Final = frozenset(
    {
        "empty",
        "malformed",
        "unmapped",
        "transport",
        "device_volume",
        "volume",
        "relative_volume",
        "other",
    }
)
_TRANSPORT_PATHS: Final = frozenset(
    {
        "/ctrl-int/1/nextitem",
        "/ctrl-int/1/previtem",
        "/ctrl-int/1/play",
        "/ctrl-int/1/playpause",
        "/ctrl-int/1/stop",
        "/ctrl-int/1/pause",
        "/ctrl-int/1/discrete-pause",
        "/ctrl-int/1/shuffle_songs",
    }
)


@dataclass
class PrivateVolumeCapture:
    """Bounded historical observations for one acknowledged private CLI process."""

    generation: int = field(default_factory=lambda: next(_GENERATIONS))
    _child: AsyncProcess | None = None
    _trusted: bool = False
    _dacp: dict[str, int] = field(default_factory=dict)
    _dacp_overflow: bool = False
    _final: dict[str, SerializableType] | None = None
    _incomplete: str | None = None
    _stdout_validated: bool = True

    def trust(self, child: AsyncProcess) -> None:
        """Bind collection to exactly the child which completed startup acknowledgement."""
        if self._child is None and not self._trusted:
            self._child = child
            self._trusted = True

    def require_stdout_validation(self) -> None:
        """Keep a terminal summary provisional until its private stdout tail is validated."""
        self._stdout_validated = False

    def finish_stdout(self, *, acknowledged: bool, complete: bool) -> None:
        """Settle validation even after stderr detached this record into bounded history."""
        self._stdout_validated = complete
        if not acknowledged:
            self._fail("revoked-acknowledgement")
        elif not complete:
            self._fail("incomplete-stdout")

    def owns(self, child: AsyncProcess | None) -> bool:
        """Match the acknowledged child identity, including its historical exit tail."""
        return self._trusted and self._child is child and child is not None

    def observe_dacp(self, classification: str, current_owner: bool) -> None:
        """Count one fixed DACP class without retaining any request content."""
        if self._final is not None or classification not in _DACP_CLASSES:
            return
        previous = self._dacp.get(classification, 0)
        if previous == _UINT64_MAX:
            self._dacp_overflow = True
        else:
            self._dacp[classification] = previous + 1
        if not current_owner:
            self._fail("stale-or-unattached")

    def mark_force_killed(self) -> None:
        """Invalidate the owning stream's record even after stderr detached its child."""
        self._fail("force-killed")

    def mark_dacp_error(self) -> None:
        """Invalidate capture after a value-bearing DACP parsing failure."""
        self._fail("malformed-dacp")

    def consume_stderr(self, child: AsyncProcess, line: str) -> bool:
        """Accept a single strict final summary only from the exact trusted child."""
        if not line.startswith(_STATUS_PREFIX):
            return False
        if not self.owns(child):
            self._fail("stale-child")
            return True
        if self._final is not None:
            self._fail("duplicate-summary")
            return True
        fields = _parse_summary(line)
        if fields is None:
            self._fail("malformed-summary")
            return True
        self._final = fields
        if fields["overflow"] or self._dacp_overflow:
            self._fail("overflow")
        elif (
            fields["event_at_cutoff"] != "connected"
            or fields["datastream_at_cutoff"] != "connected"
        ):
            self._fail("incomplete-plane-coverage")
        return True

    def end(self, child: AsyncProcess | None, reason: str) -> None:
        """Seal this historical record and drop the owned child reference."""
        if self._child is child:
            if self._final is None or reason in ("force-killed", "revoked-acknowledgement"):
                self._fail(reason)
            self._child = None

    def diagnostics(self) -> dict[str, SerializableType]:
        """Return sanitized finite history, never live identity or authority."""
        record: dict[str, SerializableType] = {
            "generation": self.generation,
            "historical_only": True,
            "current_authority": False,
            "terminal": self._final is not None,
            "interval_coverage_verified": False,
            "planes_connected_at_cutoff": self._final is not None
            and self._final["event_at_cutoff"] == "connected"
            and self._final["datastream_at_cutoff"] == "connected",
            "record_valid": self._final is not None
            and self._incomplete is None
            and self._stdout_validated,
            "dacp_counters": {key: self._dacp.get(key, 0) for key in sorted(_DACP_CLASSES)},
            "dacp_overflow": self._dacp_overflow,
        }
        if self._final is not None:
            record["cli_summary"] = self._final
        if self._incomplete is not None:
            record["incomplete"] = self._incomplete
        return record

    def _fail(self, reason: str) -> None:
        """Preserve the first finite terminal failure reason."""
        if self._incomplete is None:
            self._incomplete = reason


def _parse_summary(line: str) -> dict[str, SerializableType] | None:
    """Validate the CLI's exact fixed v1 terminal summary schema."""
    tokens = line.split()
    if tokens[:2] != ["[STATUS]", "native_volume_capture"] or len(tokens) != len(_FIELDS) + 2:
        return None
    values: dict[str, str] = {}
    for token in tokens[2:]:
        if token.count("=") != 1:
            return None
        key, value = token.split("=", 1)
        if key not in _FIELDS or key in values:
            return None
        values[key] = value
    if set(values) != set(_FIELDS) or values["version"] != "1":
        return None
    if values["dacp"] != "outside_cli" or any(
        values[key] != "0" for key in ("subscription", "publication", "capability")
    ):
        return None
    parsed: dict[str, SerializableType] = {"version": 1, "dacp": "outside_cli"}
    for key in _UINT_FIELDS:
        if _UINT_RE.fullmatch(values[key]) is None or int(values[key]) > _UINT64_MAX:
            return None
        parsed[key] = int(values[key])
    for key in _BOOL_FIELDS:
        if values[key] not in ("0", "1"):
            return None
        parsed[key] = values[key] == "1"
    for key in _PLANE_FIELDS:
        if values[key] not in ("connected", "closed", "unattached"):
            return None
        parsed[key] = values[key]
    return parsed


def classify_dacp(raw_request: bytes, mapped: bool) -> str:
    """Classify a DACP request before values are converted or actions dispatched."""
    if not raw_request:
        return "empty"
    try:
        _, path, _ = raw_request.split(b"\r\n", 1)[0].decode("ascii").split(" ")
    except UnicodeDecodeError, ValueError:
        return "malformed"
    if not mapped:
        return "unmapped"
    if path in ("/ctrl-int/1/volumeup", "/ctrl-int/1/volumedown"):
        return "relative_volume"
    if "dmcp.device-volume=" in path:
        return "device_volume"
    if "dmcp.volume=" in path:
        return "volume"
    if path in _TRANSPORT_PATHS:
        return "transport"
    return "other"
