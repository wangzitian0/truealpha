"""AST guard: forbid bare/broad exception swallows around SQL .execute() (#1774)."""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOTS = ("apps", "common")
FORBIDDEN_CATCH_NAMES = frozenset({"Exception", "BaseException"})


def _calls_execute(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and child.func.attr == "execute":
            return True
    return False


def _handler_reraises(handler: ast.ExceptHandler) -> bool:
    return any(isinstance(node, ast.Raise) for node in ast.walk(ast.Module(body=handler.body, type_ignores=[])))


def _catches_broad_type(expr: ast.expr | None) -> bool:
    if expr is None:
        return True
    if isinstance(expr, ast.Name):
        return expr.id in FORBIDDEN_CATCH_NAMES
    if isinstance(expr, ast.Tuple):
        return any(_catches_broad_type(elt) for elt in expr.elts)
    return False


def find_sql_except_swallows(source: str, filename: str = "<string>") -> list[str]:
    """Return descriptions of try/except blocks wrapping .execute() that catch broad/bare exceptions without re-raise."""
    tree = ast.parse(source, filename=filename)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        if not any(_calls_execute(stmt) for stmt in node.body):
            continue
        for handler in node.handlers:
            if _catches_broad_type(handler.type) and not _handler_reraises(handler):
                caught = ast.unparse(handler.type) if handler.type is not None else "bare except"
                offenders.append(f"{filename}:{handler.lineno}: swallows `{caught}` around .execute()")
    return offenders


def test_no_broad_except_swallows_around_sql_execute() -> None:
    offenders: list[str] = []
    for root in SRC_ROOTS:
        for path in sorted((REPO_ROOT / root).glob("*/src/**/*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            offenders.extend(find_sql_except_swallows(path.read_text(encoding="utf-8"), filename=rel))
    assert offenders == [], "Forbidden broad except without re-raise wrapping SQL .execute():\n" + "\n".join(offenders)


def test_sql_except_swallow_guard_catches_twelve_data_mutation_shape() -> None:
    swallowing = """
def _from_price_store(conn):
    try:
        return conn.execute("select 1").fetchone()
    except Exception:
        return None
"""
    assert find_sql_except_swallows(swallowing, "sample.py") == ["sample.py:5: swallows `Exception` around .execute()"]

    bare = """
def _bare(conn):
    try:
        return conn.execute("select 1").fetchone()
    except:
        pass
"""
    assert find_sql_except_swallows(bare, "sample.py") == ["sample.py:5: swallows `bare except` around .execute()"]

    narrow_or_reraised = """
def _ok(conn, log):
    try:
        return conn.execute("select 1").fetchone()
    except psycopg.errors.UndefinedTable:
        return None
    except Exception:
        log.exception("failed")
        raise
"""
    assert find_sql_except_swallows(narrow_or_reraised, "sample.py") == []
