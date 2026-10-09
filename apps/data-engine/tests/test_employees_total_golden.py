"""The development golden for `employees_total` and its exact-match test (#1142, #766, #773 D9).

Until this golden existed, nothing said how often the extracted employee count is right.
The `10k-extraction` rows in the warehouse are the extractor's own output, so they cannot
grade it. The `manual-review` rows are not labels: they carry no verified accession.

This golden holds one label per annual filing that the repository packages. The labeler read
each label from the filing text. The labeler did not copy a label from the extractor output.
A label that disagrees with the extractor stays in the file. Nobody tunes either side to make
them match.

Claim ceiling: development golden, self-reviewed, not a blind holdout (init.md rule 18).

The test runs the deterministic rule path only: `filing_plain_text`, `candidates`, and
`select_total`. These are the same functions `extract_headcount` calls before it asks a model.
The test never calls a model or a network service.

Every check below is a plain function that returns a list of errors. The real tests and the
red-proof tests call the same functions. A red-proof test changes one thing in a temporary
copy and expects the same function to report it.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, get_args

import pytest
from data_engine.datahub.standards.filing_extraction import (
    ExtractionStatus,
    candidates,
    filing_plain_text,
    select_total,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
FILINGS_RELATIVE = Path("apps") / "data-engine" / "samples" / "filings"
GOLDEN_PATH = Path(__file__).with_name("fixtures") / "employees_total.v1.json"

#: The fields of a golden row. `scope_note` is the only optional field.
REQUIRED_ROW_FIELDS = frozenset(
    {
        "ticker",
        "cik",
        "accession",
        "form",
        "fiscal_period_end",
        "sample",
        "value",
        "unit",
        "scope",
        "evidence_span",
        "expected_rule_outcome",
    }
)
OPTIONAL_ROW_FIELDS = frozenset({"scope_note"})
HEADER_FIELDS = frozenset(
    {
        "schema_version",
        "golden_id",
        "claim_ceiling",
        "size",
        "baseline_exact_match",
        "baseline_basis",
        "sample_sha256",
        "rows",
    }
)
CLAIM_CEILING = "development golden: self-reviewed, not a blind holdout (init.md rule 18)"
#: The rule path reaches a total on a row only with this outcome.
REACHABLE = "resolved"
#: Decimal places of `baseline_exact_match`.
RATE_PLACES = 4
#: The cover page opens every annual filing. The fiscal period end must appear in this many characters.
#: The measured positions in the 12 packaged filings are 210 to 310.
COVER_WINDOW = 1000
FORM_BY_SAMPLE_TAG = {"10K": "10-K", "10KA": "10-K/A", "20F": "20-F"}


# --- loading ------------------------------------------------------------------------------------


def load_golden(path: Path) -> dict[str, Any]:
    return dict(json.loads(path.read_text()))


@lru_cache(maxsize=32)
def visible_text(path: Path) -> str:
    """The visible text the extractor reads. This is `filing_plain_text` and no second HTML-to-text path."""
    return filing_plain_text(path.read_bytes())


def annual_sample_names(filings_dir: Path) -> set[str]:
    """Every packaged annual filing. The 8-K samples are not annual filings."""
    return {path.name for path in filings_dir.glob("*.html") if "8K" not in path.name}


# --- checks: each returns a list of errors, empty when the golden holds --------------------------


def shape_errors(golden: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if set(golden) != HEADER_FIELDS:
        errors.append(f"header fields differ: {sorted(set(golden) ^ HEADER_FIELDS)}")
    if golden.get("claim_ceiling") != CLAIM_CEILING:
        errors.append(f"claim_ceiling must read {CLAIM_CEILING!r}")
    rows = golden.get("rows", [])
    if golden.get("size") != len(rows):
        errors.append(f"size says {golden.get('size')} but the file holds {len(rows)} rows")
    for row in rows:
        label = row.get("sample", "<no sample>")
        fields = set(row)
        if not REQUIRED_ROW_FIELDS <= fields <= REQUIRED_ROW_FIELDS | OPTIONAL_ROW_FIELDS:
            errors.append(f"{label}: row fields differ: {sorted(fields ^ REQUIRED_ROW_FIELDS)}")
            continue
        name = Path(row["sample"]).name
        ticker, tag, digits = name.removesuffix(".html").split("_")
        accession = f"{digits[:10]}-{digits[10:12]}-{digits[12:]}"
        expectations = {
            "ticker": ticker,
            "accession": accession,
            "form": FORM_BY_SAMPLE_TAG[tag],
            "unit": "employees",
            "scope": "total",
        }
        for field, expected in expectations.items():
            if row[field] != expected:
                errors.append(f"{label}: {field} is {row[field]!r}, the sample file name says {expected!r}")
        if not re.fullmatch(r"[1-9]\d*", row["value"]):
            errors.append(f"{label}: value {row['value']!r} is not an integer string")
        if not re.fullmatch(r"\d{10}", row["cik"]):
            errors.append(f"{label}: cik {row['cik']!r} is not a 10-digit string")
        if row["expected_rule_outcome"] not in get_args(ExtractionStatus):
            errors.append(f"{label}: expected_rule_outcome {row['expected_rule_outcome']!r} is not a status")
        if str(date.fromisoformat(row["fiscal_period_end"])) != row["fiscal_period_end"]:
            errors.append(f"{label}: fiscal_period_end is not an ISO date")
    return errors


def sample_hash_errors(golden: dict[str, Any], root: Path) -> list[str]:
    errors: list[str] = []
    for relative, expected in sorted(golden["sample_sha256"].items()):
        path = root / relative
        if not path.is_file():
            errors.append(f"sample file is missing: {relative}")
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            errors.append(f"sample file changed: {relative} is {actual}, the golden says {expected}")
    return errors


def coverage_errors(golden: dict[str, Any], root: Path) -> list[str]:
    """Each packaged annual filing has exactly one row and one hash, and nothing else does."""
    packaged = {str(FILINGS_RELATIVE / name) for name in annual_sample_names(root / FILINGS_RELATIVE)}
    hashed = set(golden["sample_sha256"])
    labelled = [row["sample"] for row in golden["rows"]]
    errors: list[str] = []
    if hashed != packaged:
        errors.append(f"hash map differs from the packaged filings: {sorted(hashed ^ packaged)}")
    if sorted(labelled) != sorted(set(labelled)) or set(labelled) != packaged:
        errors.append(f"rows differ from the packaged filings: {sorted(set(labelled) ^ packaged)}")
    return errors


def _holds_value(span: str, value: int) -> bool:
    forms = (str(value), f"{value:,}")
    return any(re.search(rf"(?<![\d,]){re.escape(form)}(?!\d)(?!,\d)", span) for form in forms)


def span_errors(golden: dict[str, Any], root: Path) -> list[str]:
    errors: list[str] = []
    for row in golden["rows"]:
        text = visible_text(root / row["sample"])
        span = row["evidence_span"]
        occurrences = text.count(span)
        if occurrences != 1:
            errors.append(f"{row['sample']}: the evidence span occurs {occurrences} times, not once")
        if not _holds_value(span, int(row["value"])):
            errors.append(f"{row['sample']}: the evidence span does not state the value {row['value']}")
    return errors


def cover_errors(golden: dict[str, Any], root: Path) -> list[str]:
    """The cik and the form come from the filing's own cover tags. The period end comes from its cover text."""
    errors: list[str] = []
    for row in golden["rows"]:
        path = root / row["sample"]
        raw = path.read_bytes().decode("utf-8", "ignore")
        for tag, label in (("EntityCentralIndexKey", row["cik"]), ("DocumentType", row["form"])):
            match = re.search(rf'name="dei:{tag}"[^>]*>(.*?)</ix:nonNumeric>', raw, re.S)
            stated = re.sub(r"<[^>]+>", "", match.group(1)).strip() if match else None
            if stated != label:
                errors.append(f"{row['sample']}: the cover tag {tag} says {stated!r}, the label says {label!r}")
        period = date.fromisoformat(row["fiscal_period_end"])
        # The cover splits the date across two tags, so the visible text can read "May 5 , 2025".
        date_form = rf"{period:%B}\s+{period.day}\s*,\s*{period.year}"
        if not re.search(date_form, visible_text(path)[:COVER_WINDOW]):
            errors.append(f"{row['sample']}: the cover text does not state the period end {row['fiscal_period_end']}")
    return errors


@dataclass(frozen=True)
class RuleResult:
    status: str
    value: int | None


def rule_path(path: Path) -> RuleResult:
    """The deterministic half of `extract_headcount`: recall, then the single-candidate rule."""
    status, chosen = select_total(candidates(visible_text(path)))
    return RuleResult(status, chosen.value if chosen else None)


def outcome_errors(golden: dict[str, Any], root: Path) -> list[str]:
    """The recorded outcome is a measurement of the rule path. A row that moves is a visible edit."""
    errors: list[str] = []
    for row in golden["rows"]:
        actual = rule_path(root / row["sample"]).status
        if actual != row["expected_rule_outcome"]:
            errors.append(
                f"{row['sample']}: the rule path ends in {actual}, the golden says {row['expected_rule_outcome']}"
            )
    return errors


@dataclass(frozen=True)
class Measurement:
    matched: int
    reachable: int
    mismatches: tuple[str, ...]

    @property
    def rate(self) -> float:
        return round(self.matched / self.reachable, RATE_PLACES)


def measure_exact_match(golden: dict[str, Any], root: Path) -> Measurement:
    """Exact match over the rows where the rule path reaches a total."""
    matched = 0
    reachable = 0
    mismatches: list[str] = []
    for row in golden["rows"]:
        if row["expected_rule_outcome"] != REACHABLE:
            continue
        reachable += 1
        result = rule_path(root / row["sample"])
        if result.status == REACHABLE and result.value == int(row["value"]):
            matched += 1
        else:
            mismatches.append(f"{row['sample']}: label {row['value']}, rule path {result.status} {result.value}")
    return Measurement(matched, reachable, tuple(mismatches))


def baseline_errors(golden: dict[str, Any], measurement: Measurement) -> list[str]:
    if measurement.reachable == 0:
        return ["no row is reachable by the rule path, so the exact-match rate is undefined"]
    baseline = golden["baseline_exact_match"]
    detail = f"{measurement.matched}/{measurement.reachable} = {measurement.rate}"
    if measurement.rate < baseline:
        return [f"exact match fell below the baseline {baseline}: {detail}; {list(measurement.mismatches)}"]
    if measurement.rate > baseline:
        return [
            f"exact match rose above the baseline {baseline}: {detail}; raise baseline_exact_match in the same change"
        ]
    return []


# --- the real golden ----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def golden() -> dict[str, Any]:
    assert GOLDEN_PATH.is_file(), f"the golden is missing: {GOLDEN_PATH}"
    return load_golden(GOLDEN_PATH)


def test_the_golden_has_the_declared_shape_and_claim_ceiling(golden) -> None:
    assert shape_errors(golden) == []


def test_the_golden_covers_every_packaged_annual_filing_once(golden) -> None:
    assert coverage_errors(golden, REPO_ROOT) == []


def test_every_sample_hash_matches_the_file_on_disk(golden) -> None:
    assert sample_hash_errors(golden, REPO_ROOT) == []


def test_every_evidence_span_occurs_once_in_the_visible_text_and_states_the_value(golden) -> None:
    assert span_errors(golden, REPO_ROOT) == []


def test_the_cik_the_form_and_the_period_end_match_the_filing_cover(golden) -> None:
    assert cover_errors(golden, REPO_ROOT) == []


def test_the_rule_path_ends_in_the_recorded_outcome_for_every_row(golden) -> None:
    assert outcome_errors(golden, REPO_ROOT) == []


def test_the_exact_match_rate_equals_the_recorded_baseline(golden) -> None:
    measurement = measure_exact_match(golden, REPO_ROOT)
    assert baseline_errors(golden, measurement) == []


# --- red-proofs: one change in a temporary copy, and the same check functions must report it ------


@pytest.fixture(scope="module")
def measured_matches(golden) -> list[dict[str, Any]]:
    """Rows where the rule path reaches the label. A red-proof needs a row that is green first."""
    rows = [
        row
        for row in golden["rows"]
        if row["expected_rule_outcome"] == REACHABLE and rule_path(REPO_ROOT / row["sample"]).value == int(row["value"])
    ]
    assert rows, "no row is both reachable and equal to its label, so no red-proof can start green"
    return rows


@pytest.fixture
def mini(tmp_path, golden, measured_matches) -> tuple[dict[str, Any], Path]:
    """A one-row golden and a temporary root that holds a copy of its sample. Green by construction."""
    row = copy.deepcopy(measured_matches[0])
    sample = tmp_path / row["sample"]
    sample.parent.mkdir(parents=True)
    shutil.copy(REPO_ROOT / row["sample"], sample)
    mini_golden = {
        **copy.deepcopy(golden),
        "size": 1,
        "baseline_exact_match": 1.0,
        "sample_sha256": {row["sample"]: golden["sample_sha256"][row["sample"]]},
        "rows": [row],
    }
    assert sample_hash_errors(mini_golden, tmp_path) == []
    assert span_errors(mini_golden, tmp_path) == []
    assert cover_errors(mini_golden, tmp_path) == []
    assert outcome_errors(mini_golden, tmp_path) == []
    assert baseline_errors(mini_golden, measure_exact_match(mini_golden, tmp_path)) == []
    return mini_golden, tmp_path


def _reload(golden: dict[str, Any], root: Path) -> dict[str, Any]:
    """Write the golden to a temporary file and read it back, as the real test does."""
    path = root / "employees_total.v1.json"
    path.write_text(json.dumps(golden))
    return load_golden(path)


def test_red_proof_a_changed_label_value_fails_the_span_check_and_the_exact_match(mini) -> None:
    mini_golden, root = mini
    row = mini_golden["rows"][0]
    row["value"] = str(int(row["value"]) + 1)
    changed = _reload(mini_golden, root)
    assert len(span_errors(changed, root)) == 1
    assert len(baseline_errors(changed, measure_exact_match(changed, root))) == 1


def test_red_proof_one_altered_character_in_a_span_fails_the_span_check(mini) -> None:
    mini_golden, root = mini
    row = mini_golden["rows"][0]
    row["evidence_span"] = row["evidence_span"][:-1] + ("x" if row["evidence_span"][-1] != "x" else "y")
    changed = _reload(mini_golden, root)
    errors = span_errors(changed, root)
    assert len(errors) == 1
    assert "occurs 0 times" in errors[0]


def test_red_proof_a_missing_sample_file_fails_the_hash_check(mini) -> None:
    mini_golden, root = mini
    (root / mini_golden["rows"][0]["sample"]).unlink()
    errors = sample_hash_errors(_reload(mini_golden, root), root)
    assert len(errors) == 1
    assert "missing" in errors[0]


def test_red_proof_one_changed_byte_in_a_sample_fails_the_hash_check(mini) -> None:
    mini_golden, root = mini
    sample = root / mini_golden["rows"][0]["sample"]
    sample.write_bytes(sample.read_bytes() + b" ")
    errors = sample_hash_errors(_reload(mini_golden, root), root)
    assert len(errors) == 1
    assert "changed" in errors[0]


def test_red_proof_a_baseline_below_the_measured_rate_fails_with_a_raise_message(mini) -> None:
    mini_golden, root = mini
    mini_golden["baseline_exact_match"] = 0.5
    changed = _reload(mini_golden, root)
    errors = baseline_errors(changed, measure_exact_match(changed, root))
    assert len(errors) == 1
    assert "rose above" in errors[0]


def test_red_proof_a_measured_rate_below_the_baseline_fails_with_a_fell_message(mini) -> None:
    mini_golden, root = mini
    row = mini_golden["rows"][0]
    row["value"] = str(int(row["value"]) + 1)
    changed = _reload(mini_golden, root)
    measurement = measure_exact_match(changed, root)
    assert measurement.matched == 0
    errors = baseline_errors(changed, measurement)
    assert len(errors) == 1
    assert "fell below" in errors[0]


def test_red_proof_a_wrong_cik_a_wrong_form_and_a_wrong_period_end_fail_the_cover_check(mini) -> None:
    mini_golden, root = mini
    row = mini_golden["rows"][0]
    row["cik"] = "0000000001"
    row["form"] = "20-F"
    row["fiscal_period_end"] = "1999-01-02"
    errors = cover_errors(_reload(mini_golden, root), root)
    assert len(errors) == 3


def test_red_proof_a_row_with_a_moved_rule_outcome_fails_the_outcome_check(mini) -> None:
    mini_golden, root = mini
    mini_golden["rows"][0]["expected_rule_outcome"] = "needs_model_selection"
    errors = outcome_errors(_reload(mini_golden, root), root)
    assert len(errors) == 1
    assert "ends in resolved" in errors[0]


def test_red_proof_a_golden_with_no_reachable_row_fails_instead_of_passing_empty(mini) -> None:
    mini_golden, root = mini
    mini_golden["rows"][0]["expected_rule_outcome"] = "needs_model_selection"
    measurement = measure_exact_match(mini_golden, root)
    assert measurement.reachable == 0
    assert "undefined" in baseline_errors(mini_golden, measurement)[0]


def test_red_proof_a_dropped_row_and_a_wrong_size_fail_the_shape_and_coverage_checks(golden) -> None:
    dropped = copy.deepcopy(golden)
    dropped["rows"].pop()
    assert any("size says" in error for error in shape_errors(dropped))
    assert coverage_errors(dropped, REPO_ROOT) != []
