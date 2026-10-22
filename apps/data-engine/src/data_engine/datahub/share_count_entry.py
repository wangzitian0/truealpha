"""The vintage entry that dates a filed share count (#1277).

`share_basis.live_on_close_basis` needs the own date and the filing date of a share count. The
deployed parser writes both in the `shares_outstanding` entry of the payload `vintage`. A payload
without that entry (an old payload, or a test fixture) has only `shares_period_end`. This module is
the one rule that fills the gap. The TOPT core build (`production_topt.materialization`) and the live
strategy seed (`strategy_bridge`) both call it, so the two cannot date one payload in two ways.

The fill is exact where it can be. A missing own date is the `shares_period_end` of the payload. A
missing filing date is the date of `knowable_at`: the time at which the observation became knowable,
which is not before the filing. A payload with neither gives an entry without dates, and
`live_on_close_basis` then names the count `share_count_undated`.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

from data_engine.datahub.share_basis import SHARES_KEY


def share_count_entry(
    vintage: Mapping[str, Any] | None, shares_period_end: date | str | None, knowable_at: datetime
) -> Mapping[str, Any]:
    """The `shares_outstanding` vintage entry of a payload, with its two dates filled where the payload holds them."""
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
