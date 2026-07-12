from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from factors.composite.registered_composite_probe import registered_composite_probe
from truealpha_contracts.execution import (
    AvailabilityStatus,
    FactorInputCapability,
    ProvenanceNeutralInput,
    RequirementHandle,
)
from truealpha_contracts.universe import SubjectKind, SubjectRef

SUBJECT = SubjectRef(kind=SubjectKind.ISSUER, id="issuer:probe")
AS_OF = datetime(2026, 7, 1, tzinfo=UTC)


def _capability(seed: str, *, as_of: datetime = AS_OF) -> FactorInputCapability:
    return FactorInputCapability(
        handle=RequirementHandle(requirement_handle_id="requirement-handle:" + seed * 64),
        observation=ProvenanceNeutralInput(
            subject=SUBJECT,
            payload_model_key="contracts:ProbeSignal",
            payload_sha256=seed * 64,
            valid_from=date(2026, 1, 1),
            valid_to=date(2026, 12, 31),
            confidence=Decimal("0.8"),
            as_of=as_of,
        ),
    )


def test_composite_probe_consumes_only_same_cutoff_capabilities() -> None:
    result = registered_composite_probe(subject=SUBJECT, inputs=(_capability("a"), _capability("b")))

    assert result.availability_status is AvailabilityStatus.AVAILABLE


def test_composite_probe_rejects_mixed_cutoffs() -> None:
    with pytest.raises(ValueError, match="one snapshot cutoff"):
        registered_composite_probe(
            subject=SUBJECT,
            inputs=(_capability("a"), _capability("b", as_of=AS_OF + timedelta(seconds=1))),
        )
