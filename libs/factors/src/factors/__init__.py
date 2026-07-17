"""TrueAlpha factor library.

Layout (init.md Section 4):
- base/      factors that consume staging (incl. KG) data directly
- composite/ factors that consume other factors' mart outputs; confidence = min() of inputs
- shared/    entity resolution (KG read/write) + LLM structured-extraction primitive

Hard constraints (init.md Section 1):
- Factors never know data provenance, only confidence: every input is a
  (entity_id, value, confidence, as_of) tuple.
- Function signatures align with the eventual Dagster asset convention from Phase -1 on.
"""

from factors.confidence import evaluate_continuous_confidence, verify_confidence_calibration_report
from factors.registry import FACTOR_REGISTRY, factor
from factors.types import (
    DataAvailability,
    Fact,
    FactorError,
    FactorResult,
    GrowthConvention,
    InsufficientDataError,
)

__all__ = [
    "FACTOR_REGISTRY",
    "evaluate_continuous_confidence",
    "factor",
    "DataAvailability",
    "Fact",
    "FactorError",
    "FactorResult",
    "GrowthConvention",
    "InsufficientDataError",
    "verify_confidence_calibration_report",
]
