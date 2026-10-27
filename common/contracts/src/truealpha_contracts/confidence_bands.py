"""The one function that gives a confidence its display band (#1517 A2).

`confidence_bands.v1.json` holds the two thresholds. A reader calls `confidence_band` when it
serves a cell. No store holds the band word.
"""

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
    payload = json.loads(resources.files("truealpha_contracts.data").joinpath(CONFIDENCE_BANDS_RESOURCE).read_bytes())
    high_from, medium_from = Decimal(payload["high_from"]), Decimal(payload["medium_from"])
    if not Decimal(0) <= medium_from < high_from <= Decimal(1):
        raise ValueError(f"{CONFIDENCE_BANDS_RESOURCE}: thresholds must satisfy 0 <= medium_from < high_from <= 1")
    return high_from, medium_from


def confidence_band(confidence: Decimal | None) -> ConfidenceBand | None:
    """The display band of a confidence from 0 to 1. A cell with no confidence has no band."""
    if confidence is None:
        return None
    high_from, medium_from = _thresholds()
    if confidence >= high_from:
        return "high"
    if confidence >= medium_from:
        return "medium"
    return "low"
