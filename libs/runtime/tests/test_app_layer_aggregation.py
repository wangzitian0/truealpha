"""Unit tests for tools/check_app_layer_aggregation.py (#727, #1272).

Verifies that the App-layer aggregation guard scans mart readers, research routes,
and UI components, detects single-line and multiline SQL/JS aggregations, and ignores
comments and non-aggregate identifiers.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import check_app_layer_aggregation  # noqa: E402


def test_the_live_app_web_tree_has_no_cross_row_aggregation() -> None:
    """All configured scan roots exist, hold TypeScript files, and pass the guard."""
    assert check_app_layer_aggregation.SCAN_ROOTS == (
        check_app_layer_aggregation.MART_READERS,
        check_app_layer_aggregation.RESEARCH_ROUTES,
        check_app_layer_aggregation.COMPONENTS,
    )
    for root in check_app_layer_aggregation.SCAN_ROOTS:
        assert root.is_dir(), f"missing scan root: {root}"
        files = list(root.rglob("*.ts")) + list(root.rglob("*.tsx"))
        assert files, f"scan root {root} holds no TypeScript files (green-while-empty risk)"

    assert check_app_layer_aggregation.ALLOWED == {}
    assert check_app_layer_aggregation.offenders() == []
    assert check_app_layer_aggregation.main() == 0


@pytest.mark.parametrize(
    ("snippet", "expected_line"),
    [
        ("const sql = 'SELECT sum(weight) FROM mart.v';\n", 1),
        ("const sql = 'SELECT AVG (price) FROM mart.v';\n", 1),
        ("const sql = 'SELECT stddev(ret) FROM mart.v';\n", 1),
        ("const sql = 'SELECT VARIANCE(x) FROM mart.v';\n", 1),
        ("const sql = 'SELECT rank() OVER (PARTITION BY id) FROM mart.v';\n", 1),
        ("const header = 1;\nconst sql = `SELECT x OVER\n  (ORDER BY ts)`;\n", 2),
    ],
)
def test_each_forbidden_aggregate_is_detected_when_present(
    tmp_path: Path,
    snippet: str,
    expected_line: int,
) -> None:
    """Every forbidden aggregate pattern triggers an offender entry with its line number."""
    target = tmp_path / "bad_reader.ts"
    target.write_text(snippet, encoding="utf-8")

    found = check_app_layer_aggregation.offenders((tmp_path,))
    assert len(found) == 1
    path, line_no, _ = found[0]
    assert path == target
    assert line_no == expected_line


def test_comments_and_substring_identifiers_do_not_trigger_false_positives(tmp_path: Path) -> None:
    """Comments, URLs, count(), and prefix-sharing identifiers pass without false positives."""
    target = tmp_path / "clean_component.tsx"
    target.write_text(
        "\n".join(
            [
                "// Header comment explaining why sum(x) and avg(x) are forbidden",
                "/* Multiline block comment:",
                " * Do not write stddev(x) or variance(y) over (partition by z)",
                " */",
                "export function renderSummary(rows: number[]) {",
                "  const total = count(rows); // inline comment mentioning sum(rows)",
                "  const s = summary(rows);",
                "  const o = overall(rows);",
                "  const docs = 'https://truealpha.local/docs'; // trailing avg(x) note",
                "  return { total, s, o, docs };",
                "}",
            ]
        ),
        encoding="utf-8",
    )

    assert check_app_layer_aggregation.offenders((tmp_path,)) == []


def test_allowed_map_exempts_only_named_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An entry in ALLOWED exempts only that file while sibling violations still fail."""
    exempt = tmp_path / "exempt.ts"
    violator = tmp_path / "violator.ts"
    exempt.write_text("const q = 'SELECT sum(a) FROM t';\n", encoding="utf-8")
    violator.write_text("const q = 'SELECT avg(a) FROM t';\n", encoding="utf-8")

    monkeypatch.setitem(check_app_layer_aggregation.ALLOWED, "exempt.ts", "#727")
    found = check_app_layer_aggregation.offenders((tmp_path,))
    assert [path for path, _, _ in found] == [violator]
