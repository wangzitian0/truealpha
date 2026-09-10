"""Materialize the module-6 theme-purity rows for a governed run (#772, init.md §0 q6).

The producer half of q6: read the accepted segment partitions the standards backfill landed,
ask the seated model which segments belong to each governed theme, hand both to
`factors.base.theme_purity`, and write one `mart.issuer_theme_purity` row per (issuer, theme)
so the App reads a column instead of ranking in its own SQL (init.md §1 rule 2).

Three separations, each of which is somebody else's job on purpose:

- **Recall and acceptance** already happened (`segment_extraction`, #805/#806). This reads
  rows that a partition rule accepted against the issuer's own consolidated revenue; it does
  not re-derive that and cannot loosen it.
- **The judgement** is the model's (`llm.classify_segments`), gated, recorded and replayed
  like every other model call. A second run of the same cutoff re-asks nothing.
- **The arithmetic** is the factor's, and it is deterministic given the judgement — which is
  what lets one row be checked by hand against a filing.

Vintage selection is PIT: the newest partition per issuer whose `knowable_at` is at or before
the run's cutoff. Taking the newest partition unconditionally would apply a later filing
retroactively on a replay, the same trap `fund_consolidation` records for N-PORT weights.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from factors.base.theme_purity import ThemePurity, ThemeSegment, theme_purity
from psycopg import Connection
from truealpha_contracts.execution import AvailabilityStatus, FactorValidationStatus, InputEvidenceStatus
from truealpha_contracts.standards import confidence_for
from truealpha_contracts.theme_purity import THEMES, ThemeDefinition

from data_engine.datahub.production_topt.status_dimensions import LOW_CONFIDENCE_FLOOR
from data_engine.sources import llm

#: One issuer's newest accepted partition knowable at the cutoff, with its segments.
#:
#: `distinct on (cik)` over `knowable_at desc` picks the vintage; the join then pulls that
#: partition's rows only. A reader that took `max(knowable_at)` per ROW would mix two
#: partitions' segments into one set — the exact double-count `partition_id` exists to stop.
_PARTITION_SQL = """
with vintage as (
    select distinct on (cik) cik, partition_id, knowable_at, period_end,
           partition_total, partition_residual
    from staging.issuer_segment_revenue_facts
    where knowable_at <= %(cutoff)s
    order by cik, knowable_at desc, id desc
)
select v.cik, v.partition_id, v.period_end, v.partition_total, v.partition_residual,
       f.segment_name, f.segment_revenue, f.extractor, f.confidence, f.evidence_ref
from vintage v
join staging.issuer_segment_revenue_facts f
  on f.partition_id = v.partition_id
order by v.cik, f.segment_revenue desc, f.segment_name
"""


@dataclass(frozen=True)
class IssuerPartition:
    """One issuer's accepted segment set at the cutoff."""

    cik: int
    partition_id: str
    period_end: Any
    consolidated_revenue: Decimal
    partition_residual: Decimal
    #: (segment name, revenue), in the order the model will be asked about them.
    parts: tuple[tuple[str, Decimal], ...]
    #: What the classifier is actually shown, one per part. Usually the segment name, which
    #: is what the filing calls it and is judgeable on its own ("Semiconductor solutions").
    #:
    #: A SINGLE-SEGMENT issuer is the exception and the reason this field exists: its one
    #: part is labelled `Single operating segment`, which no classifier can judge against any
    #: theme — every pure-play would decline, fall below the coverage floor, and be refused,
    #: which is the outcome the single-segment path was built to stop. The filing's own
    #: sentence travels in `evidence_ref` precisely because it says what the company does
    #: ("providing an observability and security platform for cloud applications"), so that
    #: is what the model gets.
    descriptions: tuple[str, ...]
    accession: str
    #: Confidence of the extraction that produced the set — the ceiling on any share
    #: computed from it (init.md rule 3: composite confidence cannot exceed the minimum
    #: consumed confidence).
    extraction_confidence: Decimal

    @property
    def issuer_id(self) -> str:
        return f"issuer:cik:{self.cik:010d}"


def load_partitions(connection: Connection[Any], *, cutoff: datetime) -> tuple[IssuerPartition, ...]:
    """Every issuer with a segment partition knowable at `cutoff`, newest vintage each."""
    rows = connection.execute(_PARTITION_SQL, {"cutoff": cutoff}).fetchall()
    grouped: dict[int, list[Any]] = {}
    meta: dict[int, tuple] = {}
    for cik, partition_id, period_end, total, residual, name, revenue, _extractor, confidence, evidence in rows:
        key = int(cik)
        meta.setdefault(key, (str(partition_id), period_end, total, residual, confidence, str(evidence)))
        grouped.setdefault(key, []).append((str(name), Decimal(str(revenue))))
    partitions = []
    for cik, parts in grouped.items():
        partition_id, period_end, total, residual, confidence, evidence = meta[cik]
        partitions.append(
            IssuerPartition(
                cik=cik,
                partition_id=partition_id,
                period_end=period_end,
                consolidated_revenue=Decimal(str(total)),
                partition_residual=Decimal(str(residual)),
                parts=tuple(parts),
                descriptions=_descriptions_for(parts, evidence),
                accession=_accession_of(evidence),
                extraction_confidence=Decimal(str(confidence)),
            )
        )
    return tuple(partitions)


#: How the single-segment adapter writes the filing's own sentence onto the row.
_SINGLE_SEGMENT_MARKER = "single_segment_statement="


def _descriptions_for(parts: list[tuple[str, Decimal]], evidence_ref: str) -> tuple[str, ...]:
    """What to show the classifier for each part.

    The segment's own name for a real segment table. For the one-part partition of an issuer
    that states it has a single segment, the filing's sentence instead — the label on that
    row is `Single operating segment`, which is unjudgeable, and handing it to a model
    guarantees a decline for every pure-play.
    """
    marker = evidence_ref.find(_SINGLE_SEGMENT_MARKER)
    if len(parts) == 1 and marker != -1:
        statement = evidence_ref[marker + len(_SINGLE_SEGMENT_MARKER) :].strip()
        if statement:
            return (statement,)
    return tuple(name for name, _ in parts)


def _accession_of(evidence_ref: str) -> str:
    """The filing the set was read from, from the row's own evidence reference.

    The accession is the model invocation's replay coordinate, so it has to be the FILING's
    identity and not the run's: the same filing classified under the same theme must replay,
    and a new filing must not.
    """
    for token in evidence_ref.split():
        if token.startswith("accession="):
            return token.removeprefix("accession=")
    return evidence_ref[:64]


def _status_dimensions(
    purity: ThemePurity, definition: ThemeDefinition
) -> tuple[AvailabilityStatus, InputEvidenceStatus, FactorValidationStatus]:
    """The three §8 dimensions, derived from what this share actually consumed.

    A refused share is `unavailable` — the reason travels in `reason_codes`. A published
    share whose classified mass is partial is `degraded` source evidence: the partition was
    accepted, but part of it carries no judgement, which is the "an asserted input has no
    evidence on the row" shape #747 grades down.
    """
    if purity.result.value is None:
        return (AvailabilityStatus.UNAVAILABLE, InputEvidenceStatus.DEGRADED, FactorValidationStatus.NOT_EVALUATED)
    availability = (
        AvailabilityStatus.LOW_CONFIDENCE
        if purity.result.confidence < LOW_CONFIDENCE_FLOOR
        else AvailabilityStatus.AVAILABLE
    )
    evidence = (
        InputEvidenceStatus.VERIFIED
        if purity.unclassified_revenue == 0 and purity.partition_residual == 0
        else InputEvidenceStatus.DEGRADED
    )
    # #65 owns the holdout verdict; module 6 has no sealed record, so it is not_evaluated
    # rather than a claim this factor is not entitled to make.
    del definition
    return (availability, evidence, FactorValidationStatus.NOT_EVALUATED)


def compute_for_theme(
    connection: Connection[Any],
    partition: IssuerPartition,
    definition: ThemeDefinition,
    *,
    cutoff: datetime,
    persist_invocations: bool = True,
) -> tuple[ThemePurity, str]:
    """Classify one issuer's segments against one theme and compute the share.

    Returns the factor output and the extractor identity that judged it. The classifier is
    shown the segment NAMES and the theme definition, never the revenue — see
    `llm.classify_segments`.
    """
    classification = llm.classify_segments(
        connection,
        cik=partition.cik,
        accession=partition.accession,
        issuer_label=partition.issuer_id,
        theme=definition.theme,
        inclusion=definition.inclusion,
        segments=list(partition.descriptions),
        caller="theme-purity",
        persist=persist_invocations,
    )
    # The factor sees the segment's own NAME; only the classifier sees the description. A
    # row that recorded the sentence as its segment name would make the plane and the mart
    # disagree about what the segment is called.
    segments = [
        ThemeSegment(segment_name=name, revenue=revenue, in_theme=verdict)
        for (name, revenue), verdict in zip(partition.parts, classification.verdicts, strict=True)
    ]
    # init.md rule 3: a derived confidence cannot exceed the minimum consumed one. The
    # extraction's confidence and the model's policy confidence are both consumed here, so
    # the share carries the lower.
    model_confidence = confidence_for("segment-revenue-confidence:v1", classification.extractor)
    confidence = min(partition.extraction_confidence, model_confidence)
    # The coverage floor is the DEFINITION's and the refusal is the FACTOR's — the same
    # split module 5 uses, where `EtfConsolidationDefinition` states the floors and
    # `consolidate_fund` refuses against them. A producer that withdrew a computed share
    # afterwards would put the policy in two places and let a second consumer publish what
    # this one refused.
    purity = theme_purity(
        segments,
        entity_id=partition.issuer_id,
        theme=definition.theme,
        as_of=cutoff,
        consolidated_revenue=partition.consolidated_revenue,
        partition_residual=partition.partition_residual,
        confidence=confidence,
        minimum_classified_share=definition.minimum_classified_share,
    )
    return purity, classification.extractor


_INSERT_SQL = """
insert into mart.issuer_theme_purity
    (run_id, issuer_id, cik, theme_id, theme, definition_version, definition_sha256, cutoff,
     period_end, partition_id, theme_share, consolidated_revenue, in_theme_revenue,
     out_of_theme_revenue, unclassified_revenue, partition_residual, segments, confidence,
     reason_codes, extractor, availability_status, source_evidence_status, factor_validation_status)
values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
on conflict (run_id, issuer_id, theme_id) do update set
    -- Provenance travels with the numbers. A re-run that sees a newer partition at the same
    -- cutoff, or a reworded theme, would otherwise leave a share from one extraction beside
    -- the partition_id and definition sha of another — a row that reads as re-checkable and
    -- is not (review on #807). `fund_consolidation` refreshes its whole row for the same
    -- reason.
    cik = excluded.cik,
    theme = excluded.theme,
    definition_version = excluded.definition_version,
    definition_sha256 = excluded.definition_sha256,
    cutoff = excluded.cutoff,
    period_end = excluded.period_end,
    partition_id = excluded.partition_id,
    theme_share = excluded.theme_share,
    consolidated_revenue = excluded.consolidated_revenue,
    in_theme_revenue = excluded.in_theme_revenue,
    out_of_theme_revenue = excluded.out_of_theme_revenue,
    unclassified_revenue = excluded.unclassified_revenue,
    partition_residual = excluded.partition_residual,
    segments = excluded.segments,
    confidence = excluded.confidence,
    reason_codes = excluded.reason_codes,
    extractor = excluded.extractor,
    availability_status = excluded.availability_status,
    source_evidence_status = excluded.source_evidence_status,
    factor_validation_status = excluded.factor_validation_status
"""


def materialize_theme_purity(
    connection: Connection[Any],
    *,
    run_id: str,
    cutoff: datetime,
    themes: tuple[ThemeDefinition, ...] = tuple(THEMES.values()),
    persist_invocations: bool = True,
) -> tuple[ThemePurity, ...]:
    """Write one `mart.issuer_theme_purity` row per (issuer with a partition, theme).

    Idempotent per (run_id, issuer, theme): a re-run of the same tick replaces its own row
    rather than accumulating, since the row is a function of the run and the definition —
    and the model is replayed, not re-asked.
    """
    written: list[ThemePurity] = []
    for partition in load_partitions(connection, cutoff=cutoff):
        for definition in themes:
            purity, extractor = compute_for_theme(
                connection, partition, definition, cutoff=cutoff, persist_invocations=persist_invocations
            )
            availability, evidence, validation = _status_dimensions(purity, definition)
            connection.execute(
                _INSERT_SQL,
                (
                    run_id,
                    partition.issuer_id,
                    partition.cik,
                    definition.theme_id,
                    definition.theme,
                    definition.factor_version,
                    definition.content_sha256,
                    cutoff,
                    partition.period_end,
                    partition.partition_id,
                    purity.result.value,
                    purity.consolidated_revenue,
                    purity.in_theme_revenue,
                    purity.out_of_theme_revenue,
                    purity.unclassified_revenue,
                    purity.partition_residual,
                    purity.segments,
                    purity.result.confidence,
                    list(purity.result.flags),
                    extractor,
                    availability.value,
                    evidence.value,
                    validation.value,
                ),
            )
            written.append(purity)
    return tuple(written)


def summary_line(rows: tuple[ThemePurity, ...]) -> str:
    if not rows:
        return "theme purity: no issuer has a segment partition at this cutoff"
    published = [r for r in rows if r.result.value is not None]
    parts = [
        f"{r.entity_id.removeprefix('issuer:cik:')}/{r.theme}={r.result.value:.4f}"
        for r in sorted(published, key=lambda r: r.result.value or Decimal(0), reverse=True)[:5]
    ]
    return f"theme purity: {len(published)}/{len(rows)} published" + (
        f"; top {', '.join(parts)}" if parts else "; all refused"
    )
