"""Seeds `issuer_modules_parity.json` for Python module 3 / module 4 parity tests (#42)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CANON: dict[str, Any] = json.loads((Path(__file__).parent / "issuer_modules_parity.json").read_text())

_COMMON_COLS = (
    "confidence",
    "reason_codes",
    "extractor",
    "availability_status",
    "source_evidence_status",
    "factor_validation_status",
)
_TABLE_SPECS = (
    (
        "supply_chain_exposure",
        "mart.issuer_supply_chain_exposure",
        (
            "cutoff",
            "exposure_score",
            "direct_partners",
            "suppliers_count",
            "customers_count",
            "max_partner_share",
            *_COMMON_COLS,
        ),
    ),
    (
        "analyst_ratings",
        "mart.issuer_analyst_ratings",
        ("cutoff", "consensus_rating", "analysts_count", "buy_count", "hold_count", "sell_count", *_COMMON_COLS),
    ),
)


def seed_module_rows(connection: Any) -> None:
    issuer = CANON["issuer_id"]
    for key, table, cols in _TABLE_SPECS:
        col_list = ", ".join(("run_id", "issuer_id", *cols))
        placeholders = ", ".join(["%s"] * (len(cols) + 2))
        sql = f"insert into {table} ({col_list}) values ({placeholders})"
        for row in CANON[key]["rows"]:
            run_id = str(CANON["served_run_id"] if row["run"] == "served" else CANON["unserved_run_id"])
            connection.execute(sql, (run_id, issuer, *(row[c] for c in cols)))


def seed_served_head(connection: Any) -> None:
    p = CANON["pointer"]
    connection.execute(
        """insert into mart.current_pointer
           (pointer_id, content_sha256, environment, universe_id, universe_version, factor_id,
            target_run_id, sequence, previous_run_id, advanced_at)
           values (%s, %s, (select environment from mart.environment_identity), %s, %s, %s, %s, 0, null, now())""",
        (
            f"current-pointer:{p['pointer_sha256']}",
            p["pointer_sha256"],
            p["universe_id"],
            p["universe_version"],
            p["factor_id"],
            CANON["served_run_id"],
        ),
    )
    if (
        connection.execute("select 1 from mart.served_head where run_id = %s", (CANON["served_run_id"],)).fetchone()
        is None
    ):
        raise AssertionError("the seed must give mart.served_head a head for the served run")
