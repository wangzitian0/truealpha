"""Release-derived source adapter (Phase 3e, ADR A1 / #171).

Implements `SourceFetchPort` for the `listing-identity` and `universe-membership` semantics.
Unlike the live-source adapters, these are deterministic projections of the frozen release /
universe binding — there is no network fetch. Each work item resolves to a
`ReleaseDerivedRecord` (the canonical identity/membership payload plus the release freeze
time), and the adapter emits the Decimal-free normalized observation and its immutable raw
bytes. A record knowable only after the cutoff is a look-ahead violation.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.datahub import CaptureWorkItem
from truealpha_contracts.models import DataSource
from truealpha_contracts.obligation_reason_codes import ObligationReasonCode

from data_engine.datahub.production_topt.executor import (
    FetchFailure,
    FetchOutcome,
    FetchSuccess,
    NormalizedRecord,
    RawResponse,
)
from data_engine.datahub.production_topt.parser_identity import MAPPING_VERSION, PARSER_VERSION
from data_engine.datahub.production_topt.source_registrations import RELEASE_SEMANTICS as _RELEASE_SEMANTICS

if TYPE_CHECKING:
    from collections.abc import Sequence

    from data_engine.datahub.production_topt.source_registrations import RouteCell, RouteContext


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

    @property
    def targets(self) -> Mapping[str, ReleaseDerivedRecord]:
        """The planned cells this adapter answers for, by work item id (read-only)."""
        return MappingProxyType(self._targets)

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
        return FetchSuccess(
            raw=RawResponse(
                body=raw_bytes,
                source=DataSource.RELEASE,
                record_id=f"{record.semantic_type}:{record.subject_id}",
            ),
            normalized_sha256=canonical_sha256(record.payload),
            confidence=Decimal("1.0"),  # a frozen release projection is exact
            valid_from=record.knowable_at.date(),
            transaction_time=record.knowable_at,
            record=NormalizedRecord(
                payload=record.payload, parser_version=PARSER_VERSION, mapping_version=MAPPING_VERSION
            ),
        )


# -- registry route (#72) -----------------------------------------------------------------


def build_route(context: RouteContext, cells: Sequence[RouteCell]) -> ReleaseDerivedAdapter:
    """Identity and membership are release-frozen configuration: the record IS the
    coordinate, knowable from the partition start."""
    targets = {
        cell.work_item_id: ReleaseDerivedRecord(
            semantic_type=cell.semantic_type,
            subject_id=cell.listing_id,
            payload={
                "issuer_id": cell.issuer_id,
                "instrument_id": cell.instrument_id,
                "listing_id": cell.listing_id,
                "ticker": cell.ticker,
            },
            knowable_at=context.partition_start,
        )
        for cell in cells
    }
    return ReleaseDerivedAdapter(targets, cutoff=context.cutoff_date)
