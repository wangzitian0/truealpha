"""The governed head read aggregates only its own run's obligations (#1699).

mart.capture_status groups every capture run. A plain join to the head made Postgres
aggregate all runs before it matched one: on staging each call read 16k obligations,
spilled to disk and took 128 ms. The lateral form must filter by the head's run first.
"""

from __future__ import annotations

from typing import Any

import psycopg
from data_engine.datahub.question_coverage import GOVERNED_HEAD_SQL, GOVERNING_FACTOR


def test_the_head_read_filters_obligations_by_the_head_run(connection: psycopg.Connection[Any]) -> None:
    # With sequential scans priced out, a plan that aggregates every run still reads all
    # obligations, by a full index scan with no run filter. Only the filtered plan has an
    # index condition on run_id.
    connection.execute("set local enable_seqscan = off")
    plan = "\n".join(
        row[0]
        for row in connection.execute("explain (costs off) " + GOVERNED_HEAD_SQL, (GOVERNING_FACTOR, "universe:topt%"))
    )
    assert "Index Cond: (run_id = current_pointer.target_run_id)" in plan, plan
    assert "capture_obligations" in plan, plan
