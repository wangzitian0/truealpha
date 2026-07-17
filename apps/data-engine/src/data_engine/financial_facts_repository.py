"""Writer for the SSOT `staging.financial_facts` table (init.md Section 6).

`staging.financial_facts` has had no production writer since it was created
(`db/migrations/0006_bitemporal_lineage.sql`: "empty everywhere: staging
writers land after this"). This module is that writer's first slice —
`financial_facts_pipeline.py` is the caller that supplies `FinancialFact`
rows built from real SEC extraction (`sec_financial_facts.py`).

`unified_id` is a hard FK to `staging.kg_entities`; `ensure_kg_entity` upserts
a minimal row (idempotent) rather than assuming one already exists, since no
bootstrap/identifier-resolution step has populated this table for the sample
issuers yet.

Idempotency follows the `_put_invocation` idiom already established in
`headcount_repository.py`: insert with `on conflict ... do nothing`, and if
the row already existed, verify it matches rather than silently trusting a
prior write -- append-only means a genuine content change must be a new
vintage (new `transaction_time`/`raw_ref`/`mapping_version`), never an
in-place update.
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection
from truealpha_contracts.models import FinancialFact


def ensure_kg_entity(connection: Connection[Any], *, entity_id: str, display_name: str) -> None:
    connection.execute(
        """
        insert into staging.kg_entities (id, entity_type, display_name)
        values (%s, 'company', %s)
        on conflict (id) do nothing
        """,
        (entity_id, display_name),
    )


def write_financial_fact(connection: Connection[Any], fact: FinancialFact, *, source: str) -> bool:
    """Insert one PIT financial fact. Returns True if a new vintage landed,
    False if an identical row already existed (idempotent replay)."""

    inserted = connection.execute(
        """
        insert into staging.financial_facts (
            unified_id, metric, fiscal_period, valid_time, transaction_time,
            value, confidence, source, raw_ref, unit, source_metric,
            mapping_version, accession, form, is_restatement, recorded_at
        ) values (
            %s, %s, %s, daterange(%s, %s, '[]'), %s,
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s
        )
        on conflict (unified_id, metric, fiscal_period, transaction_time, source, raw_ref, mapping_version)
        do nothing
        returning id
        """,
        (
            fact.entity_id,
            fact.metric,
            fact.fiscal_period,
            fact.valid_from,
            fact.valid_to,
            fact.knowable_at,
            fact.value,
            fact.confidence,
            source,
            fact.raw_ref,
            fact.unit,
            fact.source_metric,
            fact.mapping_version,
            fact.accession,
            fact.form,
            fact.is_restatement,
            fact.recorded_at,
        ),
    ).fetchone()
    if inserted is not None:
        return True

    row = connection.execute(
        """
        select value, confidence, unit, source_metric, accession, form, is_restatement, recorded_at
        from staging.financial_facts
        where unified_id = %s and metric = %s and fiscal_period = %s and transaction_time = %s
          and source = %s and raw_ref = %s and mapping_version = %s
        """,
        (
            fact.entity_id,
            fact.metric,
            fact.fiscal_period,
            fact.knowable_at,
            source,
            fact.raw_ref,
            fact.mapping_version,
        ),
    ).fetchone()
    expected = (
        fact.value,
        fact.confidence,
        fact.unit,
        fact.source_metric,
        fact.accession,
        fact.form,
        fact.is_restatement,
        fact.recorded_at,
    )
    if row is None or tuple(row) != expected:
        raise ValueError(
            f"financial fact vintage already bound to different content: "
            f"{fact.entity_id}/{fact.metric}/{fact.fiscal_period}"
        )
    return False
