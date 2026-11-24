"""Capture obligation reason-code taxonomy and dispositions (#366, ADR A1)."""

from __future__ import annotations

from enum import StrEnum

from truealpha_contracts.common import ContentHash, FrozenModel, IdentifiedFrozenModel, content_id
from truealpha_contracts.reasons import dispositions


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


_CANONICAL = {ObligationReasonCode(k): ObligationDisposition(v) for k, v in dispositions().items()}


def disposition_for(code: ObligationReasonCode) -> ObligationDisposition:
    return _CANONICAL[code]


class ReasonCodeEntry(FrozenModel):
    code: ObligationReasonCode
    disposition: ObligationDisposition


class ObligationReasonCodeRegistry(IdentifiedFrozenModel):
    id_field = "registry_id"
    id_prefix = "obligation-reason-registry"
    registry_id: str = content_id("obligation-reason-registry")
    content_sha256: ContentHash = ""
    registry_version: str
    entries: tuple[ReasonCodeEntry, ...]

    def _canonicalize(self) -> None:
        codes = [e.code for e in self.entries]
        if len(codes) != len(set(codes)) or set(codes) != set(ObligationReasonCode):
            raise ValueError("reason code registry must bind each code once")

    def disposition_for(self, code: ObligationReasonCode) -> ObligationDisposition:
        for e in self.entries:
            if e.code is code:
                return e.disposition
        raise KeyError(code)

    @classmethod
    def canonical(cls) -> ObligationReasonCodeRegistry:
        return cls(
            registry_version="v2",
            entries=tuple(
                ReasonCodeEntry(code=c, disposition=d) for c, d in sorted(_CANONICAL.items(), key=lambda i: i[0].value)
            ),
        )
