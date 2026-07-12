from datetime import UTC, date, datetime
from decimal import Decimal

from factors.base.registered_semantic_probe import registered_semantic_probe
from truealpha_contracts.execution import (
    AvailabilityStatus,
    FactorInputCapability,
    ProvenanceNeutralInput,
    RequirementHandle,
)
from truealpha_contracts.universe import SubjectKind, SubjectRef


def test_probe_consumes_only_opaque_provenance_neutral_capabilities() -> None:
    subject = SubjectRef(kind=SubjectKind.ISSUER, id="issuer:probe")
    capability = FactorInputCapability(
        handle=RequirementHandle(requirement_handle_id="requirement-handle:" + "a" * 64),
        observation=ProvenanceNeutralInput(
            subject=subject,
            payload_model_key="contracts:ProbeSignal",
            payload_sha256="b" * 64,
            valid_from=date(2026, 1, 1),
            valid_to=date(2026, 12, 31),
            confidence=Decimal("0.9"),
            as_of=datetime(2026, 7, 1, tzinfo=UTC),
        ),
    )

    result = registered_semantic_probe(subject=subject, inputs=(capability,))

    assert result.availability_status is AvailabilityStatus.AVAILABLE
    assert set(capability.model_dump()) == {"handle", "observation"}
    assert set(capability.observation.model_dump()) == {
        "subject",
        "payload_model_key",
        "payload_sha256",
        "valid_from",
        "valid_to",
        "confidence",
        "as_of",
    }


def test_probe_fails_explicitly_when_the_registered_input_is_missing() -> None:
    subject = SubjectRef(kind=SubjectKind.ISSUER, id="issuer:probe")

    result = registered_semantic_probe(subject=subject, inputs=())

    assert result.availability_status is AvailabilityStatus.UNAVAILABLE
    assert result.reason_codes == ("required_probe_input_missing",)
