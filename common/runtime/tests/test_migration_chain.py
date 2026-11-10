# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import collections; import re; from datetime import datetime; from pathlib import Path; REPO_ROOT = Path(__file__).resolve().parents[3]; MIGRATIONS = REPO_ROOT / "db/migrations"
KNOWN_COLLISIONS = {"0019", "0029", "0030", "0031", "0037", "0039", "0040"}
LEGACY_NAMES: tuple[str, ...] = ( "0001_schemas.sql", "0002_staging_core.sql", "0003_ops.sql", "0004_runtime_contracts.sql", "0005_fund_holding_dual_lines.sql", "0006_bitemporal_lineage.sql", "0017_contract_objects.sql", "0018_usage_review_contracts.sql", "0019_filing_documents_stale_shape_cleanup.sql", "0019_mvp_filing_document.sql", "0020_headcount_extraction.sql", "0021_mvp_medium_domains.sql", "0022_app_identity.sql", "0023_capture_control.sql", "0024_access_policy_bindings.sql", "0025_manual_topt_capture_reads.sql", "0026_production_topt_core.sql", "0027_core_strategy_replay_mart.sql", "0028_evidence_graph.sql", "0029_datahub_quality_report.sql", "0029_principal_credentials.sql", "0030_converge_topt_gppe_uniform.sql", "0030_conversations.sql", "0031_documents.sql", "0031_strategy_run_snapshot_id.sql", "0032_strategy_backtest_inputs.sql", "0033_entity_display_resolution.sql", "0034_pipeline_trigger_requests.sql", "0035_strategy_input_coverage.sql", "0036_operating_branch_insurance.sql", "0037_issuer_cik_predecessors.sql", "0037_issuer_headcount_facts.sql", "0038_accepted_rulesets.sql", "0039_peg_module_1.sql", "0039_semantic_freshness_windows.sql", "0040_peg_rank.sql", "0040_topt_core_result_periods.sql", "0041_etf_universe_plane.sql", "0042_generalize_core_snapshot_invariants.sql", "0043_strategy_input_fiscal_period.sql", "0044_canonical_sha256_search_path.sql", "0045_canary_trigger_job.sql", "0046_fund_holdings_mart.sql", "0047_fund_holdings_valuation.sql", "0048_per_isin_ticker_crosswalk.sql", ); LANES = ("datahub", "factors", "bt", "web", "llm", "runtime"); LEGACY_RE = re.compile(r"\A(\d{4})_[a-z0-9_]+\.sql\Z"); TIMESTAMPED_RE = re.compile(r"\A(\d{8}T\d{4})_(" + "|".join(LANES) + r")_[a-z0-9_]+\.sql\Z")
def _names() -> list[str]: return sorted(path.name for path in MIGRATIONS.glob("*.sql"))
def _ordering_key(name: str) -> str:
    if match := LEGACY_RE.match(name): return match.group(1)
    if match := TIMESTAMPED_RE.match(name): return f"{match.group(1)}_{match.group(2)}"
    return name
def _by_key() -> dict[str, list[str]]:
    by_key: dict[str, list[str]] = collections.defaultdict(list)
    for name in _names(): by_key[_ordering_key(name)].append(name)
    return by_key
def test_no_new_migration_number_collides() -> None:
    collisions = {key: names for key, names in _by_key().items() if len(names) > 1}; fresh = set(collisions) - KNOWN_COLLISIONS
    assert not fresh, ( f"migration ordering key(s) {sorted(fresh)} are used more than once: " + "; ".join(f"{key} -> {collisions[key]}" for key in sorted(fresh)) + '. apply_migrations.sh replays the chain in glob order, so which of them runs first is decided by the alphabet of the suffix, not by intent. Rename the unreleased one (#576, #731)' )
    grown = {key: names for key, names in collisions.items() if key in KNOWN_COLLISIONS and len(names) > 2}
    assert not grown, ( f"known collision(s) {sorted(grown)} grew a third file: " + "; ".join(f"{key} -> {grown[key]}" for key in sorted(grown)) + ". The frozen set never extends; name the new migration by timestamp and lane (#731)" )
def test_the_frozen_collisions_are_still_collisions() -> None: by_key = _by_key(); stale = {number for number in KNOWN_COLLISIONS if len(by_key.get(number, [])) < 2}; assert not stale, ( f'{sorted(stale)} no longer collide — drop them from KNOWN_COLLISIONS so the set keeps shrinking instead of granting permission for a collision that is gone' )
def test_every_migration_takes_one_of_the_two_legal_forms() -> None:
    illegal = [name for name in _names() if not (LEGACY_RE.match(name) or TIMESTAMPED_RE.match(name))]
    assert not illegal, ( f'{illegal} match neither `00NN_<slug>.sql` (legacy, frozen) nor `YYYYMMDDTHHMM_<lane>_<slug>.sql` with lane in {LANES} (#731)' )
def test_the_sequential_form_is_closed_on_both_sides() -> None:
    on_disk = {name for name in _names() if LEGACY_RE.match(name)}; frozen = set(LEGACY_NAMES); new = sorted(on_disk - frozen)
    assert not new, ( f"{new} take the retired sequential form; name new migrations `{datetime.now().strftime('%Y%m%dT%H%M')}_<lane>_<slug>.sql` instead (lanes: {', '.join(LANES)}; see db/migrations/README.md, #731)" )
    gone = sorted(frozen - on_disk); assert not gone, ( f'{gone} are frozen legacy migrations no longer on disk — a deployed migration must never be renamed or deleted (it reorders every environment that ran it); if this was deliberate, remove the entry from LEGACY_NAMES in the same change' )
def test_timestamped_migrations_carry_a_real_utc_minute() -> None:
    bad = []
    for name in _names():
        if match := TIMESTAMPED_RE.match(name):
            try: datetime.strptime(match.group(1), "%Y%m%dT%H%M")
            except ValueError: bad.append(name)
    assert not bad, f"{bad} carry a timestamp that is not a calendar minute"
def test_every_timestamped_migration_applies_after_the_whole_legacy_chain() -> None:
    names = _names(); legacy = [name for name in names if LEGACY_RE.match(name)]; timestamped = [name for name in names if TIMESTAMPED_RE.match(name)]
    if legacy and timestamped: assert max(legacy) < min(timestamped), f"{min(timestamped)} would apply before {max(legacy)} in glob order"
    assert names == sorted(names)
