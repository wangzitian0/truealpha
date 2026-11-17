"""Date `shares_outstanding` vintage entries (#1277)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

from data_engine.datahub.share_basis import SHARES_KEY


def share_count_entry(
    vintage: Mapping[str, Any] | None, shares_period_end: date | str | None, knowable_at: datetime
) -> Mapping[str, Any]:
    """Fill missing share-count dates in `shares_outstanding` vintage (#1277)."""
    entry = (vintage or {}).get(SHARES_KEY)
    if isinstance(entry, Mapping):
        if entry.get("period_end") and entry.get("filed"):
            return entry
        merged = dict(entry)
    else:
        merged = {}
    if shares_period_end is not None and not merged.get("period_end"):
        merged["period_end"] = (
            shares_period_end if isinstance(shares_period_end, str) else shares_period_end.isoformat()
        )
    if merged.get("period_end") and not merged.get("filed"):
        merged["filed"] = knowable_at.astimezone(UTC).date().isoformat()
    return merged
