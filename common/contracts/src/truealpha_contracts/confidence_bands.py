"""The one function that gives a confidence its display band (#1517 A2)."""

from __future__ import annotations

import functools
import json
from decimal import Decimal
from importlib import resources
from typing import Literal

CONFIDENCE_BANDS_RESOURCE = "confidence_bands.v1.json"
ConfidenceBand = Literal["high", "medium", "low"]


@functools.cache
def _thresholds() -> tuple[Decimal, Decimal]:
    p = json.loads(resources.files("truealpha_contracts.data").joinpath(CONFIDENCE_BANDS_RESOURCE).read_bytes())
    hi, med = Decimal(p["high_from"]), Decimal(p["medium_from"])
    if not Decimal(0) <= med < hi <= Decimal(1):
        raise ValueError(f"{CONFIDENCE_BANDS_RESOURCE}: invalid thresholds")
    return hi, med


def confidence_band(confidence: Decimal | None) -> ConfidenceBand | None:
    if confidence is None:
        return None
    hi, med = _thresholds()
    return "high" if confidence >= hi else ("medium" if confidence >= med else "low")
