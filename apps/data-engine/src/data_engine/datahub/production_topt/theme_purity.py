"""Materialize module-6 theme-purity rows for a governed run."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Any

from factors.base.theme_purity import ThemePurity, ThemeSegment, theme_purity
from factors.shared.extraction import RULE_NO_DESCRIPTION
from psycopg import Connection
from truealpha_contracts.execution import AvailabilityStatus, FactorValidationStatus, InputEvidenceStatus
from truealpha_contracts.standards import STANDARDS, confidence_for
from truealpha_contracts.theme_purity import THEMES, ThemeDefinition

from data_engine.datahub.canonical_issuer import expand_issuer_alias_ids, is_canonical_issuer_id
from data_engine.datahub.primary_listing import choose_primary_listings
from data_engine.datahub.production_topt.status_dimensions import coverage_status_dimensions
from data_engine.sources import llm

_PARTITION_SQL = """
with vintage as (
    select distinct on (cik) cik, partition_id, knowable_at, period_end,
           partition_total, partition_residual
    from staging.issuer_segment_revenue_facts
    where knowable_at <= %(cutoff)s
      and extractor <> all(%(withdrawn)s::text[])
      and (%(ciks)s::integer[] is null or cik = any(%(ciks)s::integer[]))
    order by cik, knowable_at desc, id desc
)
select v.cik, v.partition_id, v.period_end, v.partition_total, v.partition_residual,
       f.segment_name, f.segment_revenue, f.extractor, f.confidence, f.evidence_ref
from vintage v
join staging.issuer_segment_revenue_facts f
  on f.partition_id = v.partition_id
order by v.cik, f.segment_revenue desc, f.segment_name
"""

_MEMBERS_SQL = """
select distinct g.issuer_id, v.source_record_id
from mart.topt_gppe_results g
cross join lateral unnest(g.input_observation_ids) as consumed(observation_id)
join staging.capture_normalized_observations n
  on n.observation_id = consumed.observation_id and n.semantic_type = 'financial-fact'
join raw.capture_source_vintages v
  on v.source_vintage_id = n.source_vintage_id
where g.run_id = %(run_id)s
  and v.source_record_id like 'companyfacts:CIK%%'
"""

_SINGLE_SEGMENT_MARKER = "single_segment_statement="
_SINGLE_SEGMENT_RULES = "rule:single-segment:"
NO_DESCRIPTION = "no_description"
NO_DESCRIPTION_EXTRACTOR = RULE_NO_DESCRIPTION

_INSERT_SQL = """
insert into mart.issuer_theme_purity
    (run_id, issuer_id, cik, theme_id, theme, definition_version, definition_sha256, cutoff,
     period_end, partition_id, theme_share, consolidated_revenue, in_theme_revenue,
     out_of_theme_revenue, unclassified_revenue, partition_residual, segments, confidence,
     reason_codes, extractor, availability_status, source_evidence_status, factor_validation_status)
values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
on conflict (run_id, issuer_id, theme_id) do update set
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


@dataclass(frozen=True)
class IssuerPartition:
    cik: int
    partition_id: str
    period_end: Any
    consolidated_revenue: Decimal
    partition_residual: Decimal
    parts: tuple[tuple[str, Decimal], ...]
    descriptions: tuple[str, ...]
    accession: str
    extraction_confidence: Decimal
    extractor: str = ""

    @property
    def issuer_id(self) -> str:
        return f"issuer:cik:{self.cik:010d}"


def load_partitions(
    connection: Connection[Any], *, cutoff: datetime, ciks: Collection[int] | None = None
) -> tuple[IssuerPartition, ...]:
    withdrawn = list(STANDARDS["segment_revenue"].plane.withdrawn_extractors)
    rows = connection.execute(
        _PARTITION_SQL,
        {"cutoff": cutoff, "withdrawn": withdrawn, "ciks": None if ciks is None else sorted(ciks)},
    ).fetchall()
    grouped: dict[int, list[Any]] = {}
    meta: dict[int, tuple[Any, ...]] = {}
    for cik, partition_id, period_end, total, residual, name, revenue, extractor, confidence, evidence in rows:
        key = int(cik)
        meta.setdefault(
            key, (str(partition_id), period_end, total, residual, confidence, str(evidence), str(extractor))
        )
        grouped.setdefault(key, []).append((str(name), Decimal(str(revenue))))
    return tuple(
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
            extractor=extractor,
        )
        for cik, parts in grouped.items()
        for partition_id, period_end, total, residual, confidence, evidence, extractor in (meta[cik],)
    )


def governed_members(connection: Connection[Any], *, run_id: str) -> dict[int, str]:
    claims: dict[int, set[str]] = {}
    for issuer_id, record_id in connection.execute(_MEMBERS_SQL, {"run_id": run_id}).fetchall():
        digits = str(record_id).removeprefix("companyfacts:CIK")
        if digits.isdigit():
            claims.setdefault(int(digits), set()).add(str(issuer_id))
    return {cik: next(iter(issuers)) for cik, issuers in claims.items() if len(issuers) == 1}


def _descriptions_for(parts: list[tuple[str, Decimal]], evidence_ref: str) -> tuple[str, ...]:
    marker = evidence_ref.find(_SINGLE_SEGMENT_MARKER)
    if len(parts) == 1 and marker != -1:
        statement = evidence_ref[marker + len(_SINGLE_SEGMENT_MARKER) :].strip()
        if statement:
            return (statement,)
    return tuple(name for name, _ in parts)


def undescribed(partition: IssuerPartition) -> bool:
    return (
        partition.extractor.startswith(_SINGLE_SEGMENT_RULES)
        and len(partition.parts) == 1
        and partition.descriptions == (partition.parts[0][0],)
    )


def _accession_of(evidence_ref: str) -> str:
    for token in evidence_ref.split():
        if token.startswith("accession="):
            return token.removeprefix("accession=")
    return evidence_ref[:64]


def _status_dimensions(
    purity: ThemePurity, _definition: ThemeDefinition
) -> tuple[AvailabilityStatus, InputEvidenceStatus, FactorValidationStatus]:
    return coverage_status_dimensions(
        value_present=purity.result.value is not None,
        confidence=purity.result.confidence,
        fully_evidenced=purity.unclassified_revenue == 0 and purity.partition_residual == 0,
    )


def compute_for_theme(
    connection: Connection[Any],
    partition: IssuerPartition,
    definition: ThemeDefinition,
    *,
    cutoff: datetime,
    subject_id: str | None = None,
    ticker: str | None = None,
    persist_invocations: bool = True,
) -> tuple[ThemePurity, str]:
    if undescribed(partition):
        refused = theme_purity(
            [ThemeSegment(segment_name=name, revenue=revenue, in_theme=None) for name, revenue in partition.parts],
            entity_id=subject_id or partition.issuer_id,
            theme=definition.theme,
            as_of=cutoff,
            consolidated_revenue=partition.consolidated_revenue,
            partition_residual=partition.partition_residual,
            confidence=partition.extraction_confidence,
            minimum_classified_share=definition.minimum_classified_share,
        )
        flagged = refused.result.model_copy(update={"flags": [NO_DESCRIPTION, *refused.result.flags]})
        return replace(refused, result=flagged), NO_DESCRIPTION_EXTRACTOR
    classification = llm.classify_segments(
        connection,
        cik=partition.cik,
        accession=partition.accession,
        issuer_label=f"{ticker} ({partition.issuer_id})" if ticker else partition.issuer_id,
        theme=definition.theme,
        inclusion=definition.inclusion,
        segments=list(partition.descriptions),
        caller="theme-purity",
        persist=persist_invocations,
    )
    segments = [
        ThemeSegment(segment_name=name, revenue=revenue, in_theme=verdict)
        for (name, revenue), verdict in zip(partition.parts, classification.verdicts, strict=True)
    ]
    confidence = min(
        partition.extraction_confidence,
        confidence_for("segment-revenue-confidence:v1", classification.extractor),
    )
    purity = theme_purity(
        segments,
        entity_id=subject_id or partition.issuer_id,
        theme=definition.theme,
        as_of=cutoff,
        consolidated_revenue=partition.consolidated_revenue,
        partition_residual=partition.partition_residual,
        confidence=confidence,
        minimum_classified_share=definition.minimum_classified_share,
    )
    return purity, classification.extractor


def _resolve_member_tickers(
    connection: Connection[Any],
    partitions: tuple[IssuerPartition, ...],
    members: Mapping[int, str],
    *,
    tickers: Mapping[str, str] | None,
    known_at: datetime,
) -> dict[str, str]:
    if not partitions:
        return {}
    provided = {k: v for k, v in (tickers or {}).items() if v}
    resolved: dict[str, str] = {}
    unresolved_uuids: dict[str, str] = {}
    for partition in partitions:
        subject_id = members[partition.cik]
        if subject_id in provided:
            resolved[subject_id] = provided[subject_id]
        elif partition.issuer_id in provided:
            resolved[subject_id] = provided[partition.issuer_id]
        elif is_canonical_issuer_id(subject_id):
            unresolved_uuids[subject_id] = subject_id

    if unresolved_uuids:
        if provided:
            expanded = expand_issuer_alias_ids(connection, unresolved_uuids)
            named = {
                subject_id: sorted({provided[alias] for alias in aliases if alias in provided})
                for subject_id, aliases in expanded.items()
            }
            chosen = choose_primary_listings(connection, {k: v for k, v in named.items() if v}, known_at=known_at)
            resolved.update({subject_id: ticker for subject_id, ticker in chosen.items() if ticker is not None})

        still_missing = [uid for uid in unresolved_uuids if uid not in resolved]
        if still_missing:
            rows = connection.execute(
                """
                select entity_id::text, current_ticker
                from mart.entity_identity
                where entity_id = any(%s::uuid[])
                  and current_ticker is not null
                """,
                (still_missing,),
            ).fetchall()
            for entity_id, current_ticker in rows:
                if current_ticker:
                    resolved[str(entity_id)] = str(current_ticker)
    return resolved


def materialize_theme_purity(
    connection: Connection[Any],
    *,
    run_id: str,
    cutoff: datetime,
    themes: tuple[ThemeDefinition, ...] = tuple(THEMES.values()),
    tickers: Mapping[str, str] | None = None,
    persist_invocations: bool = True,
) -> tuple[ThemePurity, ...]:
    connection.execute(
        "delete from mart.issuer_theme_purity where run_id = %s and extractor = 'lane:unvisited:v1'",
        (run_id,),
    )
    members = governed_members(connection, run_id=run_id)
    partitions = load_partitions(connection, cutoff=cutoff, ciks=members.keys())
    resolved_tickers = _resolve_member_tickers(connection, partitions, members, tickers=tickers, known_at=cutoff)
    written: list[ThemePurity] = []
    for partition in partitions:
        subject_id = members[partition.cik]
        for definition in themes:
            purity, extractor = compute_for_theme(
                connection,
                partition,
                definition,
                cutoff=cutoff,
                subject_id=subject_id,
                ticker=resolved_tickers.get(subject_id),
                persist_invocations=persist_invocations,
            )
            availability, evidence, validation = _status_dimensions(purity, definition)
            connection.execute(
                _INSERT_SQL,
                (
                    run_id,
                    subject_id,
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


def _short_id(entity_id: str) -> str:
    return entity_id.removeprefix("issuer:cik:") if entity_id.startswith("issuer:cik:") else entity_id[:8]


def summary_line(rows: tuple[ThemePurity, ...]) -> str:
    if not rows:
        return "theme purity: no issuer has a segment partition at this cutoff"
    published = [r for r in rows if r.result.value is not None]
    parts = [
        f"{_short_id(r.entity_id)}/{r.theme}={r.result.value:.4f}"
        for r in sorted(published, key=lambda r: r.result.value or Decimal(0), reverse=True)[:5]
    ]
    return f"theme purity: {len(published)}/{len(rows)} published" + (
        f"; top {', '.join(parts)}" if parts else "; all refused"
    )
