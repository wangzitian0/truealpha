"""Slice 4 gate: retire legacy wide `mart.topt_gppe_*` / `mart.topt_core_*` relations (#1730)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from truealpha_runtime.testing import isolated_test_database

REPO_ROOT = Path(__file__).resolve().parents[3]

RETIRED_RELATIONS = (
    "mart.topt_gppe_invocations",
    "mart.topt_gppe_results",
    "mart.topt_core_invocations",
    "mart.topt_core_results",
    "mart.topt_core_meta_info",
    "mart.topt_core_result_read",
)
RETIRED_FUNCTIONS = (
    "mart.validate_topt_core_result()",
    "mart.validate_topt_gppe_result()",
)
RETIRED_IDENTIFIERS_RE = re.compile(
    r"\b("
    r"topt_gppe_invocations"
    r"|topt_gppe_results"
    r"|topt_core_invocations"
    r"|topt_core_results"
    r"|topt_core_meta_info"
    r"|topt_core_result_read"
    r"|validate_topt_core_result"
    r"|validate_topt_gppe_result"
    r")\b"
)


@pytest.fixture(scope="module")
def slice4_db() -> Iterator[str]:
    with isolated_test_database("slice4", template=None) as db:
        yield str(db)


def _docstring_expr_spans(source: str) -> set[tuple[int, int]]:
    import ast

    tree = ast.parse(source)
    spans: set[tuple[int, int]] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                spans.add((first.lineno, first.end_lineno or first.lineno))
    return spans


def test_retired_topt_mart_identifiers_removed_from_runtime_code_and_schema() -> None:
    exempt_files = {
        Path(__file__).resolve(),
        (REPO_ROOT / "common/runtime/tests/test_served_head_guard.py").resolve(),
    }
    offenders: list[str] = []
    for root in (REPO_ROOT / "apps", REPO_ROOT / "common", REPO_ROOT / "db"):
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix not in {".py", ".sql", ".ts", ".tsx"}:
                continue
            resolved = path.resolve()
            if resolved in exempt_files:
                continue
            text = path.read_text(encoding="utf-8")
            if path.suffix == ".py" and "/tests/" in path.as_posix():
                doc_spans = _docstring_expr_spans(text)
                for lineno, line in enumerate(text.splitlines(), 1):
                    if line.lstrip().startswith("#") or any(s <= lineno <= e for s, e in doc_spans):
                        continue
                    for match in RETIRED_IDENTIFIERS_RE.finditer(line):
                        offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {match.group(1)}")
                continue
            for match in RETIRED_IDENTIFIERS_RE.finditer(text):
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {match.group(1)}")
    assert not offenders, "retired Slice 4 identifiers remain:\n" + "\n".join(offenders[:30])


def test_slice4_schema_has_lineage_view_and_no_retired_topt_mart_objects(slice4_db: str) -> None:
    with psycopg.connect(slice4_db) as conn:
        for retired in RETIRED_RELATIONS:
            assert conn.execute("select to_regclass(%s)", (retired,)).fetchone() == (None,), retired
        for retired_fn in RETIRED_FUNCTIONS:
            assert conn.execute("select to_regprocedure(%s)", (retired_fn,)).fetchone() == (None,), retired_fn
        assert conn.execute("select to_regclass('mart.capture_issuer_lineage')").fetchone()[0] is not None
        conn.execute("set local role mart_readonly")
        assert conn.execute("select count(*) from mart.capture_issuer_lineage").fetchone() == (0,)


def test_lineage_view_joins_the_banking_node_for_financial_issuers(slice4_db: str) -> None:
    with psycopg.connect(slice4_db) as conn:
        definition = conn.execute("select pg_get_viewdef(to_regclass('mart.capture_issuer_lineage'), true)").fetchone()[
            0
        ]
    assert "gppe_banking_tce_v1" in definition
    assert "gppe_uniform_charge_v0" in definition
