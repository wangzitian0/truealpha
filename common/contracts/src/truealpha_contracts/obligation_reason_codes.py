"""Capture obligation reason-code taxonomy and dispositions (#366, ADR A1)."""

from __future__ import annotations

from enum import StrEnum


class ObligationDisposition(StrEnum):
    STOP = "stop"
    RETRY = "retry"
    TRACE_ONLY = "trace_only"


class ObligationReasonCode(StrEnum):
    AUTH_FAILED = "auth_failed"
    CONTRACT_VIOLATION = "contract_violation"
    RELEASE_SCOPE_MISMATCH = "release_scope_mismatch"
    LOOK_AHEAD_VIOLATION = "look_ahead_violation"
    CHECKSUM_MISMATCH = "checksum_mismatch"
    TRANSIENT_NETWORK = "transient_network"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    NOT_YET_KNOWABLE = "not_yet_knowable"
    FIELD_UNAVAILABLE = "field_unavailable"
    LOW_CONFIDENCE = "low_confidence"
    DEFERRED_CAPACITY = "deferred_capacity"


_CANONICAL: dict[ObligationReasonCode, ObligationDisposition] = {
    ObligationReasonCode.AUTH_FAILED: ObligationDisposition.STOP,
    ObligationReasonCode.CONTRACT_VIOLATION: ObligationDisposition.STOP,
    ObligationReasonCode.RELEASE_SCOPE_MISMATCH: ObligationDisposition.STOP,
    ObligationReasonCode.LOOK_AHEAD_VIOLATION: ObligationDisposition.STOP,
    ObligationReasonCode.CHECKSUM_MISMATCH: ObligationDisposition.STOP,
    ObligationReasonCode.TRANSIENT_NETWORK: ObligationDisposition.RETRY,
    ObligationReasonCode.TIMEOUT: ObligationDisposition.RETRY,
    ObligationReasonCode.RATE_LIMITED: ObligationDisposition.RETRY,
    ObligationReasonCode.SERVER_ERROR: ObligationDisposition.RETRY,
    ObligationReasonCode.NOT_YET_KNOWABLE: ObligationDisposition.TRACE_ONLY,
    ObligationReasonCode.FIELD_UNAVAILABLE: ObligationDisposition.TRACE_ONLY,
    ObligationReasonCode.LOW_CONFIDENCE: ObligationDisposition.TRACE_ONLY,
    ObligationReasonCode.DEFERRED_CAPACITY: ObligationDisposition.TRACE_ONLY,
}


def disposition_for(code: ObligationReasonCode) -> ObligationDisposition:
    return _CANONICAL[code]
