"""LLM structured-extraction primitive.

Returns (value, confidence) where confidence is the LLM's self-reported 0-1 score —
a starting point, not ground truth (init.md Section 9). The multi-sample
self-consistency fallback is NOT built until real samples show the self-reported
score is unreliable.
"""

from pydantic import BaseModel, Field


class Extraction(BaseModel):
    value: float | None
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str | None = None  # verbatim snippet from the source text, for raw_ref traceability


def extract_metric(text: str, *, metric: str) -> Extraction:
    """Extract a single numeric metric from free text (e.g. headcount from a 10-K)."""
    raise NotImplementedError("Phase 2: first real use is headcount extraction")
