from pathlib import Path

from data_engine.quality.strategy_samples import _valid_price_inventory, _verified_evidence, audit_strategy_samples
from truealpha_contracts import ReadinessLevel

SAMPLE_ROOT = Path(__file__).parents[1] / "samples"


def test_current_samples_are_ready_for_local_backtest_but_not_strategy_evaluation():
    report = audit_strategy_samples(SAMPLE_ROOT)

    assert report.assessment(ReadinessLevel.TOOLCHAIN).ready
    assert report.assessment(ReadinessLevel.LOCAL_BACKTEST).ready
    assert not report.assessment(ReadinessLevel.STRATEGY_EVALUATION).ready


def test_strategy_evaluation_retains_only_its_independent_price_evidence_blockers():
    report = audit_strategy_samples(SAMPLE_ROOT)
    blockers = set(report.assessment(ReadinessLevel.STRATEGY_EVALUATION).blockers)

    assert blockers == {"prices.history", "prices.source_reconciliation"}


def test_price_history_is_measured_per_symbol(tmp_path):
    header = "Date,Open,High,Low,Close,Adj Close,Volume\n"
    row = "{day},1,1,1,1,1,1\n"
    (tmp_path / "AAA_prices.csv").write_text(header + row.format(day="2020-01-01") + row.format(day="2024-01-01"))
    (tmp_path / "BBB_prices.csv").write_text(header + row.format(day="2023-01-01") + row.format(day="2024-01-01"))

    _, spans, errors = _valid_price_inventory(sorted(tmp_path.glob("*.csv")))

    assert not errors
    assert spans["AAA"] >= 3 * 365
    assert spans["BBB"] < 3 * 365


def test_evidence_hash_mismatch_cannot_satisfy_readiness(tmp_path):
    (tmp_path / "artifact.txt").write_text("real bytes")
    coverage = {
        "evidence_cases": [
            {
                "evidence_id": "evidence.test.hash",
                "requirement_id": "test.requirement",
                "kind": "real",
                "artifact_paths": ["artifact.txt"],
                "artifact_sha256": ["0" * 64],
                "subject_entity_ids": ["company:test"],
                "assertion_ids": ["unknown.assertion"],
                "notes": "A deliberately invalid evidence case.",
            }
        ],
        "requirement_evidence": {"test.requirement": ["evidence.test.hash"]},
    }

    verified, errors = _verified_evidence(coverage, tmp_path)

    assert not verified["test.requirement"]
    assert any("hash mismatch" in error for error in errors)
