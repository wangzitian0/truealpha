import pytest
from pydantic import ValidationError
from truealpha_contracts import (
    ObligationDisposition,
    ObligationReasonCode,
    ObligationReasonCodeRegistry,
    ReasonCodeEntry,
    disposition_for,
)


def test_every_code_maps_to_exactly_one_disposition() -> None:
    registry = ObligationReasonCodeRegistry.canonical()
    for code in ObligationReasonCode:
        assert registry.disposition_for(code) is disposition_for(code)
    # The canonical registry binds every code exactly once.
    assert {entry.code for entry in registry.entries} == set(ObligationReasonCode)


def test_dispositions_are_as_specified() -> None:
    assert disposition_for(ObligationReasonCode.CHECKSUM_MISMATCH) is ObligationDisposition.STOP
    assert disposition_for(ObligationReasonCode.LOOK_AHEAD_VIOLATION) is ObligationDisposition.STOP
    assert disposition_for(ObligationReasonCode.RATE_LIMITED) is ObligationDisposition.RETRY
    assert disposition_for(ObligationReasonCode.TIMEOUT) is ObligationDisposition.RETRY
    assert disposition_for(ObligationReasonCode.FIELD_UNAVAILABLE) is ObligationDisposition.TRACE_ONLY
    assert disposition_for(ObligationReasonCode.NOT_YET_KNOWABLE) is ObligationDisposition.TRACE_ONLY


def test_registry_is_content_identified_and_deterministic() -> None:
    a = ObligationReasonCodeRegistry.canonical()
    b = ObligationReasonCodeRegistry.canonical()
    assert a.registry_id.startswith("obligation-reason-registry:")
    assert a.registry_id == b.registry_id
    assert a.content_sha256 == b.content_sha256


def test_incomplete_registry_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ObligationReasonCodeRegistry(
            registry_version="v1",
            entries=(ReasonCodeEntry(code=ObligationReasonCode.TIMEOUT, disposition=ObligationDisposition.RETRY),),
        )


def test_duplicate_code_is_rejected() -> None:
    full = ObligationReasonCodeRegistry.canonical().entries
    with pytest.raises(ValidationError):
        ObligationReasonCodeRegistry(registry_version="v1", entries=(*full, full[0]))
