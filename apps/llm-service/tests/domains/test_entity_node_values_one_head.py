"""Entity node values come from one served head when an issuer is in several universes."""

from __future__ import annotations

import pytest
from llm_service.services import entities_service

pytestmark = pytest.mark.deps

ISSUER = "issuer:cik:0000723125"
TOPT_RUN = "capture-run:" + "1" * 64
QQQ_RUN = "capture-run:" + "2" * 64
NODE_ID = "6b4ce481-f045-5503-9675-921160e2bcd2"


def _seed(conn: object, *, topt_value: str, qqq_value: str) -> None:
    heads = (
        ("universe:topt-us-2026-03-31", TOPT_RUN, "1", "2 hours"),
        ("universe:qqq-us-2026-09-30", QQQ_RUN, "2", "1 hour"),
    )
    for universe_id, run_id, digit, age in heads:
        conn.execute(  # type: ignore[attr-defined]
            "insert into staging.evidence_nodes (node_id, kind, content_sha256, valid_from, transaction_time, "
            "recorded_at) values (%s, 'capture_run', %s, '2026-03-31', now(), now())",
            (run_id, run_id.split(":", 1)[1]),
        )
        conn.execute(  # type: ignore[attr-defined]
            "insert into mart.current_pointer (pointer_id, content_sha256, environment, universe_id, "
            "universe_version, factor_id, target_run_id, sequence, previous_run_id, advanced_at) "
            "values (%s, %s, (select environment from mart.environment_identity), %s, 'v1', 'f', %s, 0, null, "
            "now() - %s::interval)",
            (f"current-pointer:{digit * 64}", digit * 64, universe_id, run_id, age),
        )
    for run_id, value in ((TOPT_RUN, topt_value), (QQQ_RUN, qqq_value)):
        for period in ("2026-09-03", "2026-11-21"):
            conn.execute(  # type: ignore[attr-defined]
                "insert into mart.metric_node_values (run_id, issuer_id, node_id, node_key, period, value, unit, "
                "availability_status, reason_codes, source_evidence_status, confidence) "
                "values (%s, %s, %s, 'capital_charge', %s, %s, 'USD', 'available', '{}', 'verified', 0.9)",
                (run_id, ISSUER, NODE_ID, period, value),
            )


def test_node_values_keep_one_row_per_node_and_period_across_served_heads() -> None:
    """Fails if the node query returns one row for each served head (2 heads -> 4 rows instead of 2)."""
    import psycopg
    from psycopg.rows import dict_row
    from truealpha_runtime.testing import isolated_test_database

    with isolated_test_database("entity_node_one_head") as db:
        db_url = str(db)
        with psycopg.connect(db_url) as conn:
            _seed(conn, topt_value="100", qqq_value="200")
            conn.commit()
        with psycopg.connect(db_url, row_factory=dict_row) as conn, conn.cursor() as cur:
            served = cur.execute(
                "select count(*) as n from mart.served_head where run_id = any(%s)", ([TOPT_RUN, QQQ_RUN],)
            ).fetchone()
            assert served is not None and served["n"] == 2, "both seeded heads must be served"
            nodes = entities_service._fetch_trace_nodes_from_cursor(cur, [ISSUER])

    assert [(n.node_key, n.period) for n in nodes] == [
        ("capital_charge", "2026-11-21"),
        ("capital_charge", "2026-09-03"),
    ]
    # The QQQ head advanced most recently, so its value wins for both periods.
    assert {n.value for n in nodes} == {"200"}


def test_entity_detail_lists_each_node_once() -> None:
    """The entity detail route reads the same node query; it must not repeat a node per universe."""
    import psycopg
    from llm_service.services.auth_service import PrincipalRecord, access_context_for
    from truealpha_runtime.testing import isolated_test_database

    with isolated_test_database("entity_detail_one_head") as db:
        db_url = str(db)
        with psycopg.connect(db_url) as conn:
            _seed(conn, topt_value="100", qqq_value="100")
            conn.commit()
        context = access_context_for(
            PrincipalRecord(principal_id="principal:t", tenant_id="tenant:t", principal_kind="administrator")
        )
        detail = entities_service.get_entity_detail(context, issuer_id=ISSUER, database_url=db_url)

    assert [(n.node_key, n.period) for n in detail.node_values] == [
        ("capital_charge", "2026-11-21"),
        ("capital_charge", "2026-09-03"),
    ]
