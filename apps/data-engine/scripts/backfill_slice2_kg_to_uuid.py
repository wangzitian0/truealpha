"""Pre-cutover backfill and parity verifier for #1730 Slice 2 (`staging.kg_*` -> UUIDs)."""

from __future__ import annotations

import argparse
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
from data_engine.config import settings
from factors.shared import entity_resolution as er


@dataclass(frozen=True)
class BackfillReport:
    entities_checked: int
    identifiers_checked: int
    edges_checked: int
    rating_refs_checked: int
    holding_refs_checked: int
    unmapped_refs: tuple[str, ...]
    applied: bool


def backfill_legacy_kg_to_uuid(conn: psycopg.Connection, *, apply: bool = False) -> BackfillReport:
    """Resolve and backfill legacy `staging.kg_*` and FK text references into canonical UUIDs."""
    has_kg_entities = conn.execute("select to_regclass('staging.kg_entities') is not null").fetchone()[0]
    has_kg_identifiers = conn.execute("select to_regclass('staging.kg_identifiers') is not null").fetchone()[0]
    has_kg_edges = conn.execute("select to_regclass('staging.kg_edges') is not null").fetchone()[0]

    raw_to_uuid: dict[str, uuid.UUID] = {}
    unmapped: list[str] = []
    now = datetime.now(UTC)

    entities_checked = 0
    if has_kg_entities:
        rows = conn.execute("select id, entity_type, display_name from staging.kg_entities order by id").fetchall()
        entities_checked = len(rows)
        for raw_id, entity_type, display_name in rows:
            raw_to_uuid[str(raw_id)] = er.ensure_entity(
                conn,
                str(raw_id),
                str(entity_type),
                str(display_name or raw_id),
                transaction_time=now,
                raw_ref="backfill:slice2",
            )

    rating_rows = conn.execute(
        """
        select distinct analyst_id::text, 'analyst'::text from staging.analyst_rating_events
        union
        select distinct company_id::text, 'company'::text from staging.analyst_rating_events
        """
    ).fetchall()
    holding_rows = conn.execute(
        """
        select distinct fund_id::text, 'etf'::text from staging.fund_holding_facts
        union
        select distinct holding_id::text, 'company'::text from staging.fund_holding_facts where holding_id is not null
        """
    ).fetchall()

    for raw_id, kind_hint in [*rating_rows, *holding_rows]:
        if raw_id in raw_to_uuid:
            continue
        resolved = er.coerce_entity_uuid(conn, raw_id)
        if resolved is None:
            resolved = er.ensure_entity(
                conn, raw_id, kind_hint, raw_id, transaction_time=now, raw_ref="backfill:slice2"
            )
        if resolved is None:
            unmapped.append(raw_id)
        else:
            raw_to_uuid[raw_id] = resolved

    identifiers_checked = 0
    if has_kg_identifiers:
        id_rows = conn.execute(
            """
            select entity_id, identifier_type, identifier_value, valid_time, transaction_time, confidence, source, raw_ref
            from staging.kg_identifiers
            order by transaction_time, id
            """
        ).fetchall()
        identifiers_checked = len(id_rows)
        for raw_id, id_type, id_val, valid_time, tx_time, confidence, source, raw_ref in id_rows:
            target = raw_to_uuid.get(str(raw_id)) or er.coerce_entity_uuid(conn, str(raw_id))
            if target is None:
                unmapped.append(f"kg_identifiers:{raw_id}")
                continue
            valid_from = (
                valid_time.lower.isoformat() if valid_time is not None and valid_time.lower is not None else "-infinity"
            )
            er.assert_identifier(
                conn,
                entity_id=target,
                source=str(source),
                identifier_type=str(id_type),
                identifier_value=str(id_val),
                confidence=float(confidence),
                transaction_time=tx_time,
                valid_from=valid_from,
                raw_ref=str(raw_ref),
            )

    edges_checked = 0
    if has_kg_edges:
        edge_rows = conn.execute(
            """
            select from_id, to_id, relation_type, valid_time, transaction_time, confidence, source, raw_ref
            from staging.kg_edges
            order by transaction_time, id
            """
        ).fetchall()
        edges_checked = len(edge_rows)
        for from_id, to_id, rel_type, valid_time, tx_time, confidence, source, raw_ref in edge_rows:
            from_uuid = raw_to_uuid.get(str(from_id)) or er.coerce_entity_uuid(conn, str(from_id))
            to_uuid = raw_to_uuid.get(str(to_id)) or er.coerce_entity_uuid(conn, str(to_id))
            if from_uuid is None or to_uuid is None or from_uuid == to_uuid:
                continue
            valid_from = (
                valid_time.lower.isoformat() if valid_time is not None and valid_time.lower is not None else "-infinity"
            )
            valid_to = valid_time.upper.isoformat() if valid_time is not None and valid_time.upper is not None else None
            er.add_edge(
                conn,
                from_id=from_uuid,
                to_id=to_uuid,
                relation_type=str(rel_type),
                confidence=float(confidence),
                source=str(source) or "slice2:migration",
                transaction_time=tx_time,
                valid_from=valid_from,
                valid_to=valid_to,
                raw_ref=str(raw_ref) or "backfill:slice2",
            )

    if unmapped or not apply:
        conn.rollback()
    else:
        conn.commit()

    return BackfillReport(
        entities_checked=entities_checked,
        identifiers_checked=identifiers_checked,
        edges_checked=edges_checked,
        rating_refs_checked=len(rating_rows),
        holding_refs_checked=len(holding_rows),
        unmapped_refs=tuple(sorted(set(unmapped))),
        applied=bool(apply and not unmapped),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill Slice 2 staging.kg_* and FK identities to UUIDs.")
    parser.add_argument("--apply", action="store_true", help="Commit changes instead of rolling back a dry-run.")
    args = parser.parse_args()

    with psycopg.connect(
        settings.database_url,
        connect_timeout=settings.database_connect_timeout_seconds,
        autocommit=False,
    ) as conn:
        report = backfill_legacy_kg_to_uuid(conn, apply=args.apply)

    mode = "APPLIED" if report.applied else ("DRY-RUN" if not args.apply else "ABORTED")
    print(
        f"[{mode}] entities={report.entities_checked} identifiers={report.identifiers_checked} "
        f"edges={report.edges_checked} rating_refs={report.rating_refs_checked} "
        f"holding_refs={report.holding_refs_checked} unmapped={len(report.unmapped_refs)}"
    )
    if report.unmapped_refs:
        for item in report.unmapped_refs[:20]:
            print(f"  UNMAPPED: {item}")
        sys.exit(1)


if __name__ == "__main__":
    main()
