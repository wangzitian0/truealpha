"""Capture obligation reason-code taxonomy and dispositions (#366, ADR A1).

Capture completeness is error-code governance, not a binary pass/fail. Every obligation
reaches a terminal state carrying a classified reason code, and each code binds exactly one
disposition the executor acts on:

- STOP        — fatal; halt the run (auth, contract/schema, release/scope mismatch,
                look-ahead, raw-blob checksum mismatch).
- RETRY       — bounded transient failure (network/timeout, HTTP 429/5xx); after N attempts
                the executor escalates to STOP or resolves `unavailable`.
- TRACE_ONLY  — record and continue (not-yet-knowable/pending, field absent for this issuer,
                low-confidence source).

The registry is source-neutral: generic capture/manifest/lineage code never branches on
source or semantic type. A run succeeds when all obligations are terminally resolved with no
STOP outstanding — not only when all are `available`. See #366 and ADR A1.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from truealpha_contracts.common import canonical_sha256


class ObligationDisposition(StrEnum):
    STOP = "stop"
    RETRY = "retry"
    TRACE_ONLY = "trace_only"


class ObligationReasonCode(StrEnum):
    # STOP — the run is invalid.
    AUTH_FAILED = "auth_failed"
    CONTRACT_VIOLATION = "contract_violation"
    RELEASE_SCOPE_MISMATCH = "release_scope_mismatch"
    LOOK_AHEAD_VIOLATION = "look_ahead_violation"
    CHECKSUM_MISMATCH = "checksum_mismatch"
    # RETRY — bounded transient failure.
    TRANSIENT_NETWORK = "transient_network"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    # TRACE_ONLY — record and continue.
    NOT_YET_KNOWABLE = "not_yet_knowable"
    FIELD_UNAVAILABLE = "field_unavailable"
    LOW_CONFIDENCE = "low_confidence"


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
}


def disposition_for(code: ObligationReasonCode) -> ObligationDisposition:
    """The single disposition bound to a reason code."""
    return _CANONICAL[code]


class ReasonCodeEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: ObligationReasonCode
    disposition: ObligationDisposition


class ObligationReasonCodeRegistry(BaseModel):
    """Versioned registry binding every reason code to exactly one disposition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

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
        payload = self.model_dump(mode="json", exclude={"registry_id", "content_sha256"})
        expected_hash = canonical_sha256(payload)
        expected_id = f"obligation-reason-registry:{expected_hash}"
        if self.content_sha256 and self.content_sha256 != expected_hash:
            raise ValueError("content_sha256 does not match canonical content")
        if self.registry_id and self.registry_id != expected_id:
            raise ValueError("registry_id does not match canonical content")
        object.__setattr__(self, "content_sha256", expected_hash)
        object.__setattr__(self, "registry_id", expected_id)
        return self

    def disposition_for(self, code: ObligationReasonCode) -> ObligationDisposition:
        for entry in self.entries:
            if entry.code is code:
                return entry.disposition
        raise KeyError(code)

    @classmethod
    def canonical(cls) -> ObligationReasonCodeRegistry:
        """The frozen v1 registry."""
        entries = tuple(
            ReasonCodeEntry(code=code, disposition=disposition)
            for code, disposition in sorted(_CANONICAL.items(), key=lambda item: item[0].value)
        )
        return cls(registry_version="v1", entries=entries)
