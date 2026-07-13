import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest
from data_engine.batches.mvp_capture_tiny.e0_slice import (
    CUTOFF,
    FixtureRawLedger,
    _latest_annual_gross_profit,
    run_e0_slice,
)
from pydantic import ValidationError
from truealpha_contracts.capture_contracts import (
    CaptureCell,
    CaptureManifest,
    CaptureRecordEvidence,
    evaluate_capture_manifest,
)
from truealpha_contracts.execution import NormalizedRecordRef

REPOSITORY_ROOT = next(
    parent
    for parent in Path(__file__).resolve().parents
    if (parent / "governance" / "vision-issue-graph.json").is_file()
)


@pytest.fixture(scope="module")
def result():
    return run_e0_slice(REPOSITORY_ROOT)


def test_e0_executes_frozen_raw_to_runner_vertical_slice(result):
    assert result.corpus_sha256 == "a36514fa9f0f4a7906879ae9a18569f294f2122babdde22dad2170a30d98abea"
    assert result.raw_entry.envelope.object.sha256 == hashlib.sha256(result.raw_capture.body).hexdigest()
    assert result.payload.metric == "gross_profit"
    assert result.payload.value.as_tuple().exponent == 0
    assert result.payload.value == 153_463_000_000
    assert result.payload.fiscal_period == "FY2026"
    assert result.payload.accession == "0001045810-26-000021"
    assert result.record.confidence.as_tuple().exponent == -2
    assert result.record.confidence == Decimal("0.99")
    assert result.record.raw_object_id == result.raw_entry.raw_id
    assert result.capture_evaluation.ready
    assert result.capture_evaluation.blocking_reason_codes == ()
    assert result.snapshot.normalized_records == (result.record,)
    assert result.runner_selection.bindings[0].input_id == result.record.normalized_record_id


def test_raw_ledger_is_idempotent_and_appends_changed_vintage(result):
    ledger = FixtureRawLedger()

    first = ledger.append(result.raw_capture)
    identical = ledger.append(result.raw_capture)
    changed = ledger.append(result.raw_capture.model_copy(update={"body": result.raw_capture.body + b"\n"}))

    assert identical == first
    assert changed.raw_id != first.raw_id
    assert len(ledger.entries) == 2
    assert first in ledger.entries


def test_repeated_slice_has_stable_content_identities():
    ledger = FixtureRawLedger()

    first = run_e0_slice(REPOSITORY_ROOT, raw_ledger=ledger)
    second = run_e0_slice(REPOSITORY_ROOT, raw_ledger=ledger)

    assert len(ledger.entries) == 1
    assert second.raw_entry == first.raw_entry
    assert second.record.normalized_record_id == first.record.normalized_record_id
    assert second.capture_manifest.capture_manifest_id == first.capture_manifest.capture_manifest_id
    assert second.snapshot.snapshot_id == first.snapshot.snapshot_id
    assert second.runner_selection.selection_id == first.runner_selection.selection_id


def _evaluation_without(result, *missing_fields: str):
    source = result.capture_manifest.cells[0].evidence[0]
    evidence_values = source.model_dump(mode="python", exclude={"evidence_id", "content_sha256"})
    evidence_values.update(dict.fromkeys(missing_fields))
    evidence = CaptureRecordEvidence(**evidence_values)
    source_cell = result.capture_manifest.cells[0]
    cell = CaptureCell(
        **source_cell.model_dump(mode="python", exclude={"capture_cell_id", "content_sha256", "evidence"}),
        evidence=(evidence,),
    )
    manifest = CaptureManifest(
        **result.capture_manifest.model_dump(
            mode="python",
            exclude={"capture_manifest_id", "content_sha256", "cells"},
        ),
        cells=(cell,),
    )
    return evaluate_capture_manifest(
        result.scope,
        manifest,
        applicability_catalog_id=result.scope.applicability_catalog_id,
        applicability_catalog_sha256=result.scope.applicability_catalog_sha256,
        applicability=result.applicability,
        source_coverage=result.source_coverage,
        evaluated_at=manifest.created_at,
    )


@pytest.mark.parametrize(
    ("missing_fields", "blocker"),
    [
        (("raw_id", "raw_sha256"), "evidence.missing_raw_id"),
        (("normalized_id",), "evidence.missing_normalized_id"),
        (("confidence",), "evidence.missing_confidence"),
    ],
)
def test_row_complete_evaluation_fails_closed(result, missing_fields, blocker):
    evaluation = _evaluation_without(result, *missing_fields)

    assert not evaluation.ready
    assert any(reason.startswith(blocker) for reason in evaluation.blocking_reason_codes)


def test_normalized_record_cannot_omit_confidence(result):
    values = result.record.model_dump(
        mode="python",
        exclude={"normalized_record_id", "content_sha256"},
    )
    values["confidence"] = None

    with pytest.raises(ValidationError):
        NormalizedRecordRef(**values)


def test_runner_projection_is_provenance_neutral(result):
    observation = result.runner_selection.factor_inputs[0].observation

    assert set(observation.model_dump()) == {
        "subject",
        "payload_model_key",
        "payload_sha256",
        "valid_from",
        "valid_to",
        "confidence",
        "as_of",
    }
    assert not ({"source", "raw_id", "raw_sha256", "recorded_at", "mapping_version"} & set(observation.model_dump()))


def test_frozen_corpus_rejects_changed_artifact_bytes(tmp_path):
    source = tmp_path / "source.json"
    source.write_text("{}", encoding="utf-8")
    artifact = tmp_path / "artifact.json"
    artifact.write_text("{}", encoding="utf-8")
    corpus = {
        "schema_version": 1,
        "corpus_id": "tampered",
        "source_manifest": {
            "path": "source.json",
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        },
        "artifacts": [
            {
                "artifact_id": "nvda-company-facts",
                "path": "artifact.json",
                "sha256": "0" * 64,
            }
        ],
        "cases": [{"case_id": f"case-{index}"} for index in range(8)],
    }
    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(json.dumps(corpus), encoding="utf-8")

    with pytest.raises(ValueError, match="artifact bytes drifted"):
        run_e0_slice(tmp_path, corpus_path=Path("corpus.json"))


def test_company_facts_schema_drift_fails_with_explicit_error():
    with pytest.raises(ValueError, match="company-facts schema drifted"):
        _latest_annual_gross_profit(b"{}", CUTOFF)


def test_frozen_corpus_rejects_missing_case_id(tmp_path):
    source = tmp_path / "source.json"
    source.write_text("{}", encoding="utf-8")
    artifact = tmp_path / "artifact.json"
    artifact.write_text("{}", encoding="utf-8")
    cases = [{"case_id": f"case-{index}"} for index in range(8)]
    cases[0] = {"case_id": None}
    corpus = {
        "schema_version": 1,
        "corpus_id": "missing-case-id",
        "source_manifest": {
            "path": "source.json",
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        },
        "artifacts": [
            {
                "artifact_id": "nvda-company-facts",
                "path": "artifact.json",
                "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            }
        ],
        "cases": cases,
    }
    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(json.dumps(corpus), encoding="utf-8")

    with pytest.raises(ValueError, match="case IDs are incomplete"):
        run_e0_slice(tmp_path, corpus_path=Path("corpus.json"))


@pytest.mark.parametrize(
    ("missing_target", "expected_error"),
    [
        ("source", "source manifest path is missing"),
        ("artifact", "artifact path is missing"),
    ],
)
def test_frozen_corpus_requires_explicit_paths(tmp_path, missing_target, expected_error):
    source = tmp_path / "source.json"
    source.write_text("{}", encoding="utf-8")
    artifact = tmp_path / "artifact.json"
    artifact.write_text("{}", encoding="utf-8")
    corpus = {
        "schema_version": 1,
        "corpus_id": "missing-path",
        "source_manifest": {
            "path": None if missing_target == "source" else "source.json",
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        },
        "artifacts": [
            {
                "artifact_id": "nvda-company-facts",
                "path": None if missing_target == "artifact" else "artifact.json",
                "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            }
        ],
        "cases": [{"case_id": f"case-{index}"} for index in range(8)],
    }
    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(json.dumps(corpus), encoding="utf-8")

    with pytest.raises(ValueError, match=expected_error):
        run_e0_slice(tmp_path, corpus_path=Path("corpus.json"))


def test_batch_remains_outside_default_release_composition():
    shared_surfaces = (
        "apps/data-engine/src/data_engine/__init__.py",
        "apps/data-engine/src/data_engine/sources/__init__.py",
        "apps/data-engine/src/data_engine/mvp_probe.py",
        "apps/data-engine/src/data_engine/contract_repository.py",
        "apps/data-engine/src/data_engine/contract_assets.py",
    )

    for relative_path in shared_surfaces:
        assert "mvp_capture_tiny" not in (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")
