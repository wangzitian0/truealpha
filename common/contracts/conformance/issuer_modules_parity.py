"""Seeds `issuer_modules_parity.json` for the Python halves of the module 3 / module 4 parity (#42).

The reader test (`common/contracts/tests/test_issuer_modules_mart.py`) and the MCP tool test
(`apps/llm-service/tests/test_mcp_module_tools.py`) load this file by path and seed the same
rows; the App half (`apps/app-web/tests/issuer-modules-parity-conformance.test.ts`) seeds the
same canon in TypeScript. Test support only: no deployed module imports it.

The caller owns the transaction and rolls it back. `session_replication_role = replica` must
be set first, so the pointer can name a capture run without the evidence-graph rows behind it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CANON: dict[str, Any] = json.loads((Path(__file__).parent / "issuer_modules_parity.json").read_text())


def _run_id(label: str) -> str:
    return str(CANON["served_run_id"] if label == "served" else CANON["unserved_run_id"])


def seed_module_rows(connection: Any) -> None:
    """The served rows and the newer unserved decoy rows of both modules."""
    issuer = CANON["issuer_id"]
    for row in CANON["supply_chain_exposure"]["rows"]:
        connection.execute(
            """insert into mart.issuer_supply_chain_exposure
               (run_id, issuer_id, cutoff, exposure_score, direct_partners, suppliers_count, customers_count,
                max_partner_share, confidence, reason_codes, extractor, availability_status,
                source_evidence_status, factor_validation_status)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                _run_id(row["run"]), issuer, row["cutoff"], row["exposure_score"], row["direct_partners"],
                row["suppliers_count"], row["customers_count"], row["max_partner_share"], row["confidence"],
                row["reason_codes"], row["extractor"], row["availability_status"],
                row["source_evidence_status"], row["factor_validation_status"],
            ),
        )  # fmt: skip
    for row in CANON["analyst_ratings"]["rows"]:
        connection.execute(
            """insert into mart.issuer_analyst_ratings
               (run_id, issuer_id, cutoff, consensus_rating, analysts_count, buy_count, hold_count, sell_count,
                confidence, reason_codes, extractor, availability_status, source_evidence_status,
                factor_validation_status)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                _run_id(row["run"]), issuer, row["cutoff"], row["consensus_rating"], row["analysts_count"],
                row["buy_count"], row["hold_count"], row["sell_count"], row["confidence"], row["reason_codes"],
                row["extractor"], row["availability_status"], row["source_evidence_status"],
                row["factor_validation_status"],
            ),
        )  # fmt: skip


def seed_served_head(connection: Any) -> None:
    """A governed pointer to the served run, in this database's own environment."""
    pointer = CANON["pointer"]
    connection.execute(
        """insert into mart.current_pointer
           (pointer_id, content_sha256, environment, universe_id, universe_version, factor_id,
            target_run_id, sequence, previous_run_id, advanced_at)
           values (%s, %s, (select environment from mart.environment_identity), %s, %s, %s, %s, 0, null, now())""",
        (
            f"current-pointer:{pointer['pointer_sha256']}",
            pointer["pointer_sha256"],
            pointer["universe_id"],
            pointer["universe_version"],
            pointer["factor_id"],
            CANON["served_run_id"],
        ),
    )
    served = connection.execute(
        "select 1 from mart.served_head where run_id = %s", (CANON["served_run_id"],)
    ).fetchone()
    if served is None:
        raise AssertionError("the seed must give mart.served_head a head for the served run")
