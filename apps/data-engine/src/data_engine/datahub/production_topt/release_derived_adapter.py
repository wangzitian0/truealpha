"""Release-derived source adapter (Phase 3e, ADR A1 / #171).

Implements `SourceFetchPort` for the `listing-identity` and `universe-membership` semantics.
Unlike the live-source adapters, these are deterministic projections of the frozen release /
universe binding — there is no network fetch. Each work item resolves to a
`ReleaseDerivedRecord` (the canonical identity/membership payload plus the release freeze
time), and the adapter emits the Decimal-free normalized observation and its immutable raw
bytes. A record knowable only after the cutoff is a look-ahead violation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from truealpha_contracts import ObligationReasonCode, canonical_sha256
from truealpha_contracts.datahub import CaptureWorkItem

from data_engine.datahub.production_topt.executor import FetchFailure, FetchOutcome, FetchSuccess

_RELEASE_SEMANTICS = frozenset({"listing-identity", "universe-membership"})


@dataclass(frozen=True)
class ReleaseDerivedRecord:
    """One frozen listing-identity or universe-membership projection."""

    semantic_type: str
    subject_id: str
    payload: dict[str, Any]
    knowable_at: datetime

    def __post_init__(self) -> None:
        if self.semantic_type not in _RELEASE_SEMANTICS:
            raise ValueError(f"unsupported release-derived semantic: {self.semantic_type}")


class ReleaseDerivedAdapter:
    """`SourceFetchPort` for release-derived semantics; no network."""

    def __init__(self, targets: dict[str, ReleaseDerivedRecord], *, cutoff: date) -> None:
        self._targets = targets
        self._cutoff = cutoff

    def fetch(self, work_item: CaptureWorkItem) -> FetchOutcome:
        record = self._targets.get(work_item.work_item_id)
        if record is None:
            return FetchFailure(ObligationReasonCode.CONTRACT_VIOLATION)
        if record.knowable_at.date() > self._cutoff:
            return FetchFailure(ObligationReasonCode.LOOK_AHEAD_VIOLATION)
        raw_bytes = json.dumps(
            {
                "semantic_type": record.semantic_type,
                "subject_id": record.subject_id,
                "payload": record.payload,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        normalized_sha256 = canonical_sha256(
            {
                "semantic_type": record.semantic_type,
                "subject_id": record.subject_id,
                "payload": record.payload,
            }
        )
        return FetchSuccess(
            raw_sha256=hashlib.sha256(raw_bytes).hexdigest(),
            object_uri=f"release-derived://{record.semantic_type}/{record.subject_id}",
            normalized_sha256=normalized_sha256,
            confidence=Decimal("1.0"),  # a frozen release projection is exact
            valid_from=record.knowable_at.date(),
            transaction_time=record.knowable_at,
        )
