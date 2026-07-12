"""Isolated Gate 0 probe for additive registered semantic inputs."""

from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.execution import (
    AvailabilityStatus,
    FactorInputCapability,
    FactorOutputDraft,
    FactorValidationStatus,
)
from truealpha_contracts.universe import SubjectRef

from factors.registry import factor

PROBE_OUTPUT_SCHEMA_SHA256 = canonical_sha256(
    {
        "type": "object",
        "properties": {"input_payload_sha256": {"type": "array", "items": {"type": "string"}}},
        "required": ["input_payload_sha256"],
        "additionalProperties": False,
    }
)


@factor("registered_semantic_probe", kind="base", module=7)
def registered_semantic_probe(
    *,
    subject: SubjectRef,
    inputs: tuple[FactorInputCapability, ...],
    output_key: str = "registered-semantic-probe",
) -> FactorOutputDraft:
    """Exercise a new typed input without inspecting its private handle binding."""

    observations = tuple(item.observation for item in inputs)
    if any(observation.subject != subject for observation in observations):
        raise ValueError("probe inputs must belong to the requested subject")
    payload_sha256 = canonical_sha256(sorted(item.payload_sha256 for item in observations))
    if not observations:
        return FactorOutputDraft(
            output_key=output_key,
            subject=subject,
            output_model_key="contracts:RegisteredSemanticProbe",
            output_schema_sha256=PROBE_OUTPUT_SCHEMA_SHA256,
            output_payload_sha256=payload_sha256,
            availability_status=AvailabilityStatus.UNAVAILABLE,
            factor_validation_status=FactorValidationStatus.NOT_EVALUATED,
            reason_codes=("required_probe_input_missing",),
        )
    return FactorOutputDraft(
        output_key=output_key,
        subject=subject,
        output_model_key="contracts:RegisteredSemanticProbe",
        output_schema_sha256=PROBE_OUTPUT_SCHEMA_SHA256,
        output_payload_sha256=payload_sha256,
        availability_status=AvailabilityStatus.AVAILABLE,
        factor_validation_status=FactorValidationStatus.NOT_EVALUATED,
    )
