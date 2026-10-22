"""Tests for private native-volume observation parsing."""

from __future__ import annotations

from unittest.mock import MagicMock

from music_assistant.providers.airplay.private_volume_capture import (
    PrivateVolumeCapture,
    classify_dacp,
)

_SUMMARY = (
    "[STATUS] native_volume_capture version=1 records=0 overflow=0 event_in=0 event_out=0 "
    "datastream_in=0 datastream_out=0 volume_get=0 volume_result=0 volume_set_candidate=0 "
    "volume_feedback=0 volume_availability=0 volume_capabilities=0 transport=0 other=0 "
    "parser_valid=0 parser_invalid=0 parser_unknown=0 event_at_cutoff=connected "
    "datastream_at_cutoff=connected dacp=outside_cli subscription=0 publication=0 capability=0 "
    "generation_at_cutoff=0 binding_at_cutoff=0 sender_candidate=0 endpoint_sender_match=0 "
    "output_sender_match=0 session_candidate=0 endpoint_session_match=0 output_session_match=0 "
    "group_candidate=0 endpoint_group_match=0 output_group_match=0"
)


def _child() -> MagicMock:
    """Make a live exact child identity seam."""
    return MagicMock(closed=False)


def test_summary_is_historical_and_does_not_authorize_actions() -> None:
    """A trusted terminal record is historical evidence, never current authority."""
    child = _child()
    capture = PrivateVolumeCapture()
    capture.trust(child)

    assert capture.consume_stderr(child, _SUMMARY)
    capture.end(child, "missing-summary")
    result = capture.diagnostics()
    assert result["record_valid"] is True
    assert result["current_authority"] is False
    summary = result["cli_summary"]
    assert isinstance(summary, dict)
    assert summary["dacp"] == "outside_cli"


def test_stdout_validation_settles_detached_history_without_child_reference() -> None:
    """A summary is provisional until stdout EOF, and detached evidence remains revocable."""
    child = _child()
    capture = PrivateVolumeCapture()
    capture.require_stdout_validation()
    capture.trust(child)
    assert capture.consume_stderr(child, _SUMMARY)
    capture.end(child, "missing-summary")
    assert capture._child is None
    assert capture.diagnostics()["record_valid"] is False

    capture.finish_stdout(acknowledged=True, complete=True)
    assert capture.diagnostics()["record_valid"] is True
    assert capture.diagnostics()["current_authority"] is False
    capture.finish_stdout(acknowledged=False, complete=False)
    assert capture.diagnostics()["record_valid"] is False
    assert capture.diagnostics()["incomplete"] == "revoked-acknowledgement"
    assert capture._child is None


def test_stale_child_duplicate_overflow_and_planes_are_not_valid() -> None:
    """Terminal failure modes never become a no-traffic proof."""
    child = _child()
    capture = PrivateVolumeCapture()
    capture.trust(child)
    assert capture.consume_stderr(_child(), _SUMMARY)
    assert capture.diagnostics()["record_valid"] is False
    assert capture.diagnostics()["incomplete"] == "stale-child"

    overflow = PrivateVolumeCapture()
    overflow.trust(child)
    assert overflow.consume_stderr(child, _SUMMARY.replace("overflow=0", "overflow=1"))
    assert overflow.diagnostics()["incomplete"] == "overflow"

    planes = PrivateVolumeCapture()
    planes.trust(child)
    assert planes.consume_stderr(
        child, _SUMMARY.replace("event_at_cutoff=connected", "event_at_cutoff=closed")
    )
    assert planes.diagnostics()["planes_connected_at_cutoff"] is False
    assert planes.diagnostics()["interval_coverage_verified"] is False


def test_force_kill_invalidates_a_previously_valid_summary() -> None:
    """Force-killed capture cannot be used as a graceful final record."""
    child = _child()
    capture = PrivateVolumeCapture()
    capture.trust(child)
    assert capture.consume_stderr(child, _SUMMARY)
    capture.end(child, "force-killed")
    assert capture.diagnostics()["record_valid"] is False
    assert capture.diagnostics()["incomplete"] == "force-killed"


def test_dacp_classification_discards_request_values() -> None:
    """DACP aggregates fixed classes before numeric conversion or dispatch."""
    assert (
        classify_dacp(b"GET /ctrl-int/1/setproperty?dmcp.volume=secret HTTP/1.1", True) == "volume"
    )
    assert classify_dacp(b"GET /ctrl-int/1/unknown HTTP/1.1", True) == "other"
    assert classify_dacp(b"not an http request", True) == "malformed"
    assert classify_dacp(b"GET /ctrl-int/1/play HTTP/1.1", False) == "unmapped"
