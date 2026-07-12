"""Composite contract probe used by the Gate 1 execution spine."""

from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.execution import (
    AvailabilityStatus,
    FactorInputCapability,
    FactorOutputDraft,
    FactorValidationStatus,
)
from truealpha_contracts.universe import SubjectRef

from factors.registry import factor

COMPOSITE_PROBE_OUTPUT_SCHEMA_SHA256 = canonical_sha256(
    {
        "type": "object",
        "properties": {"upstream_payload_sha256": {"type": "array", "items": {"type": "string"}}},
        "required": ["upstream_payload_sha256"],
        "additionalProperties": False,
    }
)


@factor("registered_composite_probe", kind="composite", module=7)
def registered_composite_probe(
    *,
    subject: SubjectRef,
    inputs: tuple[FactorInputCapability, ...],
    output_key: str = "registered-composite-probe",
) -> FactorOutputDraft:
    """Combine sanitized upstream outputs without observing runner metadata."""

    observations = tuple(item.observation for item in inputs)
    if any(observation.subject != subject for observation in observations):
        raise ValueError("composite probe inputs must belong to the requested subject")
    if len({observation.as_of for observation in observations}) > 1:
        raise ValueError("composite probe inputs must share one snapshot cutoff")
    payload_sha256 = canonical_sha256(sorted(item.payload_sha256 for item in observations))
    if not observations:
        return FactorOutputDraft(
            output_key=output_key,
            subject=subject,
            output_model_key="contracts:RegisteredCompositeProbe",
            output_schema_sha256=COMPOSITE_PROBE_OUTPUT_SCHEMA_SHA256,
            output_payload_sha256=payload_sha256,
            availability_status=AvailabilityStatus.UNAVAILABLE,
            factor_validation_status=FactorValidationStatus.NOT_EVALUATED,
            reason_codes=("required_upstream_probe_missing",),
        )
    return FactorOutputDraft(
        output_key=output_key,
        subject=subject,
        output_model_key="contracts:RegisteredCompositeProbe",
        output_schema_sha256=COMPOSITE_PROBE_OUTPUT_SCHEMA_SHA256,
        output_payload_sha256=payload_sha256,
        availability_status=AvailabilityStatus.AVAILABLE,
        factor_validation_status=FactorValidationStatus.NOT_EVALUATED,
    )
