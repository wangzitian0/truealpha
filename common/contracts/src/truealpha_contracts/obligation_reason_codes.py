"""Capture obligation reason-code taxonomy and dispositions (#366, ADR A1)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from truealpha_contracts.common import FrozenModel, identify


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


class ReasonCodeEntry(FrozenModel):
    code: ObligationReasonCode
    disposition: ObligationDisposition


class ObligationReasonCodeRegistry(FrozenModel):
    registry_id: str = Field(default="", pattern=r"^(?:|obligation-reason-registry:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    registry_version: str
    entries: tuple[ReasonCodeEntry, ...]

    @model_validator(mode="after")
    def freeze_and_identify(self) -> ObligationReasonCodeRegistry:
        codes = [entry.code for entry in self.entries]
        if len(codes) != len(set(codes)):
            raise ValueError("a reason code cannot appear twice in the registry")
        if set(codes) != set(ObligationReasonCode):
            raise ValueError("the registry must bind every reason code exactly once")
        identify(self, id_field="registry_id", prefix="obligation-reason-registry")
        return self

    def disposition_for(self, code: ObligationReasonCode) -> ObligationDisposition:
        for entry in self.entries:
            if entry.code is code:
                return entry.disposition
        raise KeyError(code)

    @classmethod
    def canonical(cls) -> ObligationReasonCodeRegistry:
        entries = tuple(
            ReasonCodeEntry(code=code, disposition=disposition)
            for code, disposition in sorted(_CANONICAL.items(), key=lambda item: item[0].value)
        )
        return cls(registry_version="v2", entries=entries)
