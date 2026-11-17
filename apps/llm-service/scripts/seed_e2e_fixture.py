"""Seed throwaway CI E2E Postgres fixture data (principals, golden runs, entity display, module outputs)."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
from pathlib import Path

import bcrypt
import psycopg
from truealpha_contracts.environment_contract import load_environment_contract
from truealpha_contracts.universes import universe_with_role

REPO_ROOT = Path(__file__).resolve().parents[3]
CANON_PATH = REPO_ROOT / "common/contracts/tests/fixtures/strategy_run_parity.json"
PREVIEW_PATH = REPO_ROOT / "common/runtime/tests/fixtures/strategy_run_preview.v1.json"
E2E_TENANT_ID = "tenant:e2e"
E2E_PRINCIPALS = (
    ("principal:e2e:member", "member@e2e.invalid", "member"),
    ("principal:e2e:admin", "admin@e2e.invalid", "administrator"),
)
PREVIEW_CUTOFF = "2026-03-31T23:59:59Z"
PREVIEW_ISSUERS = {
    "issuer:adm": (
        "11111111-1111-5111-8111-111111111101",
        "issuer:lei:549300ADM00000000001",
        "XNYS:ADM",
        "ADM",
        "Archer-Daniels-Midland",
    ),
    "issuer:ddog": (
        "11111111-1111-5111-8111-111111111102",
        "issuer:lei:549300DDOG0000000002",
        "XNAS:DDOG",
        "DDOG",
        "Datadog",
    ),
    "issuer:jpm": (
        "11111111-1111-5111-8111-111111111103",
        "issuer:lei:549300JPM00000000003",
        "XNYS:JPM",
        "JPM",
        "JPMorgan Chase",
    ),
    "issuer:nice": (
        "11111111-1111-5111-8111-111111111104",
        "issuer:lei:549300NICE0000000004",
        "XNAS:NICE",
        "NICE",
        "NICE Ltd.",
    ),
    "issuer:shop": (
        "11111111-1111-5111-8111-111111111105",
        "issuer:lei:549300SHOP0000000005",
        "XNYS:SHOP",
        "SHOP",
        "Shopify",
    ),
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hex() -> str:
    return secrets.token_hex(32)


def _seed_observation(
    conn: psycopg.Connection,
    *,
    capture_run_id: str,
    schedule_policy_id: str,
    semantic_type: str,
    knowable_at: str,
    payload: dict[str, object],
) -> str:
    obligation_id = f"capture-list-obligation:{_hex()}"
    source_request_id = f"source-request:{_hex()}"
    work_item_id = f"capture-work-item:{_hex()}"
    source_vintage_id = f"source-vintage:{_hex()}"
    observation_id = f"normalized-observation:{_hex()}"
    retryable = ["interrupted", "rate_limited", "server_error", "transport_error"]
    terminal = ["failed", "success", "unavailable", "unchanged"]
    conn.execute(
        "insert into raw.capture_obligations (obligation_id, campaign_id, run_id, list_version_id, subject_kind, subject_id, capture_requirement_id, partition_key, content_sha256) "
        "values (%s, %s, %s, %s, 'listing', 'listing:test:trace', %s, '2026-03-31', %s)",
        (
            obligation_id,
            f"capture-campaign:{_hex()}",
            capture_run_id,
            f"list-version:{_hex()}",
            f"{semantic_type}:v1",
            _hex(),
        ),
    )
    conn.execute(
        "insert into raw.capture_source_requests (source_request_id, content_sha256, source_registry_entry_id, source_policy_id, request_fingerprint_version, canonical_request_sha256, subject_refs, capture_requirement_ids, partition_key, payload) "
        "values (%s, %s, %s, 'source-policy:trace-test-v1', 'trace-test:v1', %s, '[{\"kind\":\"listing\",\"id\":\"listing:test:trace\"}]'::jsonb, %s, '2026-03-31', '{}'::jsonb)",
        (source_request_id, _hex(), f"source-registry-entry:{_hex()}", _hex(), [f"{semantic_type}:v1"]),
    )
    conn.execute(
        "insert into raw.capture_work_items (work_item_id, campaign_id, source_request_id, schedule_policy_id, maximum_attempts, retryable_outcomes, terminal_outcomes, content_sha256, storage_envelope_sha256) "
        "values (%s, %s, %s, %s, 3, %s, %s, %s, %s)",
        (
            work_item_id,
            f"capture-campaign:{_hex()}",
            source_request_id,
            schedule_policy_id,
            retryable,
            terminal,
            _hex(),
            _hex(),
        ),
    )
    conn.execute(
        "insert into raw.capture_obligation_work_bindings (binding_id, obligation_id, work_item_id, content_sha256) values (%s, %s, %s, %s)",
        (f"capture-obligation-work-binding:{_hex()}", obligation_id, work_item_id, _hex()),
    )
    conn.execute(
        "insert into raw.capture_source_vintages (source_vintage_id, content_sha256, source_request_id, source_record_id, raw_object_id, raw_fetch_id, payload) values (%s, %s, %s, 'trace-test-record', %s, 1, '{}'::jsonb)",
        (source_vintage_id, _hex(), source_request_id, f"raw-object:{_hex()}"),
    )
    conn.execute(
        "insert into staging.capture_normalized_observations (observation_id, content_sha256, capture_obligation_id, source_vintage_id, semantic_type, semantic_version, subject_kind, subject_id, valid_from, knowable_at, parser_version, mapping_version, normalized_payload_sha256, is_restatement, confidence, freshness_state, payload, recorded_at) "
        "values (%s, %s, %s, %s, %s, 'v1', 'listing', 'listing:test:trace', %s::timestamptz, %s::timestamptz, 'trace-test-parser:v1', 'trace-test-mapping:v1', %s, false, 0.85, 'fresh', '{}'::jsonb, %s::timestamptz + interval '1 day')",
        (
            observation_id,
            _hex(),
            obligation_id,
            source_vintage_id,
            semantic_type,
            knowable_at,
            knowable_at,
            _hex(),
            knowable_at,
        ),
    )
    conn.execute(
        "insert into staging.capture_observation_obligations (capture_obligation_id, observation_id) values (%s, %s)",
        (obligation_id, observation_id),
    )
    conn.execute(
        "insert into staging.capture_observation_payloads (observation_id, normalized_payload_sha256, normalized_payload) values (%s, %s, %s::jsonb)",
        (observation_id, _hex(), json.dumps(payload)),
    )
    return observation_id


def _seed_trace_lineage(
    conn: psycopg.Connection, preview_run_id: str, executed_at: str, first_selected_entity_id: str
) -> None:
    if conn.execute(
        "select 1 from mart.strategy_run_capture_bindings where strategy_run_id = %s", (preview_run_id,)
    ).fetchone():
        return
    snapshot_id = f"topt-core-snapshot:{_sha(f'e2e-preview-snapshot:{preview_run_id}')}"
    for entity_id, _legacy_id, _mic, ticker, _name in PREVIEW_ISSUERS.values():
        sym = ticker.lower()
        conn.execute(
            "insert into staging.topt_core_snapshot_members (snapshot_id, instrument_id, issuer_id, listing_id, observation_ids, member_sha256, factor_input) "
            "values (%s, %s, %s, %s, %s, %s, '{}'::jsonb) on conflict (snapshot_id, issuer_id) do nothing",
            (
                snapshot_id,
                f"instrument:{sym}",
                entity_id,
                f"listing:xnas:{sym}",
                ["obs:1", "obs:2", "obs:3", "obs:4"],
                _sha(f"member:{entity_id}"),
            ),
        )
    capture_run_id = f"capture-run:{_hex()}"
    schedule_policy_id = f"schedule-policy:{_hex()}"
    conn.execute(
        "insert into raw.capture_schedule_policies (schedule_policy_id, content_sha256, policy_version, demanded_cadence, provider_availability_cadence, freshness_max_age, retry_policy, payload) "
        "values (%s, %s, 'trace-test:v1', interval '1 day', 'manual-only:v1', interval '30 days', '{}'::jsonb, '{}'::jsonb)",
        (schedule_policy_id, _hex()),
    )
    matched = next((v for v in PREVIEW_ISSUERS.values() if v[0] == first_selected_entity_id), None)
    sym = matched[3].lower() if matched else "adm"
    listing_id, instrument_id = f"listing:xnas:{sym}", f"instrument:{sym}"
    identity = {"issuer_id": first_selected_entity_id, "instrument_id": instrument_id, "listing_id": listing_id}

    def _filing(acc: str, pend: str, filed: str) -> dict[str, object]:
        return {
            "accession": acc,
            "document": None,
            "filed": filed,
            "form": "10-K",
            "fp": "FY",
            "fy": 2025,
            "period_end": pend,
            "statement_form": True,
        }

    obs_ids = sorted(
        [
            _seed_observation(
                conn,
                capture_run_id=capture_run_id,
                schedule_policy_id=schedule_policy_id,
                semantic_type="financial-fact",
                knowable_at="2026-02-03T00:00:00Z",
                payload={
                    **identity,
                    "operating_branch": "non_financial",
                    "currency": "USD",
                    "gross_profit": "210000000",
                    "total_assets": "200000000",
                    "headcount": "100",
                    "revenue": "100000000",
                    "shares_outstanding": "10000000",
                    "pre_provision_profit": None,
                    "vintage": {
                        "gross_profit": _filing("0000320193-26-000010", "2025-12-31", "2026-02-01"),
                        "total_assets": _filing("0000320193-26-000010", "2025-12-31", "2026-02-01"),
                        "revenue": _filing("0000320193-26-000011", "2025-09-30", "2026-01-15"),
                        "shares_outstanding": _filing("0000320193-26-000012", "2025-10-17", "2026-02-03"),
                        "headcount": {
                            "accession": None,
                            "evidence_ref": "annual report p. 4",
                            "filed": None,
                            "knowable_at": "2026-01-20T00:00:00+00:00",
                            "period_end": "2025-06-30",
                            "raw": "raw.fetches:7",
                        },
                    },
                },
            ),
            _seed_observation(
                conn,
                capture_run_id=capture_run_id,
                schedule_policy_id=schedule_policy_id,
                semantic_type="market-price",
                knowable_at="2026-03-30T21:00:00Z",
                payload={**identity, "currency": "USD", "close": "40"},
            ),
            _seed_observation(
                conn,
                capture_run_id=capture_run_id,
                schedule_policy_id=schedule_policy_id,
                semantic_type="listing-identity",
                knowable_at="2026-01-01T00:00:00Z",
                payload={**identity, "ticker": sym.upper()},
            ),
            _seed_observation(
                conn,
                capture_run_id=capture_run_id,
                schedule_policy_id=schedule_policy_id,
                semantic_type="universe-membership",
                knowable_at="2026-01-01T00:00:00Z",
                payload={**identity, "ticker": sym.upper()},
            ),
        ]
    )
    strategy_universe = f"{load_environment_contract().record(universe_with_role('strategy')).id_prefix}seed"
    result_sha = _hex()
    conn.execute(
        "insert into mart.topt_core_results (result_id, content_sha256, invocation_id, snapshot_id, run_id, release_manifest_id, universe_id, universe_version, universe_sha256, cutoff, issuer_id, instrument_id, listing_id, operating_branch, operating_metric, availability, operating_efficiency, capital_adjusted_gross_profit, gppe, tier, target_ps_lower, target_ps_upper, target_ps_midpoint, current_ps, valuation_gap, confidence, freshness, reason_codes, input_observation_ids, gppe_invocation_id, gppe_result_id, gppe_definition_id, gppe_definition_sha256, tier_definition_id, tier_definition_sha256, payload, created_at, availability_status, source_evidence_status, factor_validation_status) "
        "values (%s, %s, %s, %s, %s, %s, %s, 'v1', %s, %s::timestamptz, %s, %s, %s, 'non_financial', 'capital_adjusted_gppe', 'available', 12.5, 1, 12.5, 'tech', 15, 20, 18.75, 20, 0.5, 0.85, 'fresh', '{}', %s, %s, %s, %s, %s, %s, %s, '{}'::jsonb, %s::timestamptz, 'available', 'verified', 'not_evaluated')",
        (
            f"topt-core-result:{result_sha}",
            result_sha,
            f"topt-core-invocation:{_hex()}",
            f"topt-core-snapshot:{_hex()}",
            capture_run_id,
            f"release-manifest:{_hex()}",
            strategy_universe,
            _hex(),
            PREVIEW_CUTOFF,
            first_selected_entity_id,
            instrument_id,
            listing_id,
            obs_ids,
            f"topt-gppe-invocation:{_hex()}",
            f"topt-gppe-result:{_hex()}",
            f"gppe-definition:{_hex()}",
            _hex(),
            f"three-tier-definition:{_hex()}",
            _hex(),
            executed_at,
        ),
    )
    conn.execute(
        "insert into mart.strategy_run_capture_bindings (capture_run_id, strategy_run_id, bound_by) values (%s, %s, 'tick')",
        (capture_run_id, preview_run_id),
    )


def main() -> int:
    principals_only = "--principals-only" in sys.argv
    db_url = os.environ.get("DATABASE_URL")
    password = os.environ.get("E2E_PASSWORD", "")
    if not db_url or len(password) < 12:
        print("DATABASE_URL and E2E_PASSWORD (>=12 chars) are required", file=sys.stderr)
        return 1
    pw_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=10)).decode("ascii")
    canon = json.loads(CANON_PATH.read_text(encoding="utf-8"))["report"]
    preview = json.loads(PREVIEW_PATH.read_text(encoding="utf-8"))
    corpus_sha = preview["corpus_sha256"]
    preview_run_id = f"strategy-run:{_sha(f'e2e-preview-run:{corpus_sha}:{PREVIEW_CUTOFF}')}"

    with psycopg.connect(db_url) as conn:
        conn.execute("insert into app.tenants (tenant_id) values (%s) on conflict do nothing", (E2E_TENANT_ID,))
        for pid, email, kind in E2E_PRINCIPALS:
            conn.execute(
                "insert into app.principals (principal_id, tenant_id, principal_kind) values (%s, %s, %s) on conflict (principal_id) do nothing",
                (pid, E2E_TENANT_ID, kind),
            )
            conn.execute(
                "insert into app.principal_credentials (principal_id, email, hashed_password) values (%s, %s, %s) "
                "on conflict (principal_id) do update set email = excluded.email, hashed_password = excluded.hashed_password",
                (pid, email, pw_hash),
            )
        if principals_only:
            conn.commit()
            return 0

        for run_dict, decisions in (
            (canon["run"], canon["decisions"]),
            (
                {
                    "strategy_run_id": preview_run_id,
                    "content_sha256": _sha(f"e2e-preview-content:{preview['corpus_sha256']}"),
                    "strategy_key": preview["strategy_id"],
                    "strategy_version": canon["run"]["strategy_version"],
                    "definition_content_sha256": canon["run"]["definition_content_sha256"],
                    "corpus_sha256": preview["corpus_sha256"],
                    "claim_ceiling": "preview",
                    "executed_at": "2027-06-02T00:00:00Z",
                },
                [
                    d
                    | {
                        "issuer_id": PREVIEW_ISSUERS[d["issuer_id"]][0],
                        "content_sha256": _sha(
                            json.dumps(d | {"entity_id": PREVIEW_ISSUERS[d["issuer_id"]][0]}, sort_keys=True)
                        ),
                        "strategy_decision_id": f"strategy-decision:{_sha(json.dumps(d | {'entity_id': PREVIEW_ISSUERS[d['issuer_id']][0]}, sort_keys=True))}",
                    }
                    for d in preview["decisions"]
                    if d["cutoff_at"] == PREVIEW_CUTOFF and d["issuer_id"] in PREVIEW_ISSUERS
                ],
            ),
        ):
            conn.execute(
                "insert into mart.strategy_runs (strategy_run_id, content_sha256, strategy_key, strategy_version, "
                "definition_content_sha256, corpus_sha256, claim_ceiling, executed_at) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s) on conflict (strategy_run_id) do nothing",
                (
                    run_dict["strategy_run_id"],
                    run_dict["content_sha256"],
                    run_dict["strategy_key"],
                    run_dict["strategy_version"],
                    run_dict["definition_content_sha256"],
                    run_dict["corpus_sha256"],
                    run_dict["claim_ceiling"],
                    run_dict["executed_at"],
                ),
            )
            for d in decisions:
                conn.execute(
                    "insert into mart.strategy_decisions (strategy_decision_id, content_sha256, strategy_run_id, issuer_id, "
                    "cutoff_at, capital_adjusted_labor_efficiency, tier, current_price_to_sales, target_price_to_sales, "
                    "valuation_gap, eligible, outcome, exclusion_reason, rank, target_weight, "
                    "current_price_to_gross_profit, target_price_to_gross_profit) "
                    "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "on conflict (strategy_decision_id) do nothing",
                    (
                        d["strategy_decision_id"],
                        d["content_sha256"],
                        run_dict["strategy_run_id"],
                        d["issuer_id"],
                        d["cutoff_at"],
                        d.get("capital_adjusted_labor_efficiency"),
                        d.get("tier"),
                        d.get("current_price_to_sales"),
                        d.get("target_price_to_sales"),
                        d.get("valuation_gap"),
                        d["eligible"],
                        d["outcome"],
                        d.get("exclusion_reason"),
                        d.get("rank"),
                        d.get("target_weight"),
                        d.get("current_price_to_gross_profit"),
                        d.get("target_price_to_gross_profit"),
                    ),
                )

        conn.execute("set local session_replication_role = replica")
        for entity_id, legacy_id, mic_ticker, _ticker, display_name in PREVIEW_ISSUERS.values():
            conn.execute(
                "insert into staging.entities (entity_id, kind, mint_rule, minted_by, birth_scheme, birth_value) "
                "values (%s, 'issuer', 'uuidv5:v1', 'e2e-seed', 'lei', %s) on conflict (entity_id) do nothing",
                (entity_id, legacy_id),
            )
            conn.execute(
                "insert into staging.entity_aliases (entity_id, scheme, value, valid_from, transaction_time, source, raw_ref, method, confidence, mapping_version) "
                "values (%s, 'legacy-id', %s, '2026-01-01', '2026-01-01T00:00:00Z', 'e2e-seed', 'e2e-seed', 'asserted', 1, 'e2e-seed:v1'), "
                "(%s, 'mic-ticker', %s, '2026-01-01', '2026-01-01T00:00:00Z', 'e2e-seed', 'e2e-seed', 'asserted', 1, 'e2e-seed:v1') on conflict do nothing",
                (entity_id, legacy_id, entity_id, mic_ticker),
            )
            conn.execute(
                "insert into staging.kg_entities (id, entity_type, display_name) values (%s, 'company', %s) on conflict (id) do nothing",
                (entity_id, display_name),
            )
            conn.execute(
                "insert into mart.issuer_analyst_ratings (run_id, issuer_id, cutoff, consensus_rating, analysts_count, buy_count, hold_count, sell_count, confidence, reason_codes, extractor, availability_status, source_evidence_status, factor_validation_status) "
                "values (%s, %s, %s, 4.35, 18, 12, 5, 1, 0.88, %s, 'origin:moomoo:v1', 'available', 'verified', 'accepted') on conflict (run_id, issuer_id) do nothing",
                (preview_run_id, entity_id, PREVIEW_CUTOFF, ["target_price:195.00", "consensus:buy"]),
            )
            conn.execute(
                "insert into mart.issuer_supply_chain_exposure (run_id, issuer_id, cutoff, exposure_score, direct_partners, suppliers_count, customers_count, max_partner_share, confidence, reason_codes, extractor, availability_status, source_evidence_status, factor_validation_status) "
                "values (%s, %s, %s, 0.72, 6, 3, 3, 0.34, 0.85, %s, 'graph:kg-edges:v1', 'available', 'verified', 'accepted') on conflict (run_id, issuer_id) do nothing",
                (preview_run_id, entity_id, PREVIEW_CUTOFF, ["supplier:tsmc", "customer:msft"]),
            )
        selected_rows = sorted(
            (
                d
                for d in preview["decisions"]
                if d["cutoff_at"] == PREVIEW_CUTOFF and d["issuer_id"] in PREVIEW_ISSUERS and d["outcome"] == "selected"
            ),
            key=lambda d: (d.get("rank") or 999, PREVIEW_ISSUERS[d["issuer_id"]][0]),
        )
        if selected_rows:
            _seed_trace_lineage(
                conn, preview_run_id, "2027-06-02T00:00:00Z", PREVIEW_ISSUERS[selected_rows[0]["issuer_id"]][0]
            )
        conn.commit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
