"""A consumer reads the served head, and nothing else computes a head age (#1062).

`mart.served_head` is the one read point: it carries the age, the limit, the label and the
availability of every governed head. A consumer that reads `mart.current_pointer_head`,
`mart.current_pointer` or `mart.governed_strategy_run` gets a head with no age. A consumer that
subtracts a clock from `advanced_at` makes a second definition of age that can disagree.

The scope is what the guard must govern, not what passes today: every file under the three
consumer-reachable trees. Three rules read them, with no database:

  A  no SQL statement reads a pointer relation; it reads `mart.served_head`
  B  a file that reads a head-addressed result relation has a statement on `mart.served_head`
  C  no file computes a head age (`now() - advanced_at`, `extract(epoch from (now()`,
     `STALE_AFTER`, a clock call in a file that names `advanced_*`)

Plus one rule on a scratch migrated database: every `mart` relation with a `run_id` column is
either served through the head or named with a reason in NOT_SERVED.

The guard is a RATCHET. Readers that exist today are listed in BASELINE, each with the follow-up
PR that moves it. A violation that is not in the baseline fails. A baseline entry that no longer
violates fails too, so the list can only shrink. The match is by rule, file and pattern, not by
line number, so an unrelated edit above a statement does not break it.
"""

from __future__ import annotations

import ast
import io
import os
import re
import tokenize
import uuid
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import sql
from truealpha_runtime.testing import apply_migration_chain, skip_or_fail

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Code that a consumer reaches: the Web App, the LLM service and the contracts both import.
#: `apps/data-engine/src` and `tools/` are not consumers. They may read the pointer. That stays
#: sound only while no consumer imports them, and `test_no_consumer_imports_the_data_engine`
#: holds that line.
CONSUMER_ROOTS = ("apps/app-web/src", "apps/llm-service/src", "libs/contracts/src")

TS_SUFFIXES = {".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"}
SKIP_DIRECTORIES = {"node_modules", ".next", "__pycache__", "dist", "build"}

#: Rule A: relations that hold a head with no age.
POINTER_RELATIONS = ("mart.current_pointer_head", "mart.current_pointer", "mart.governed_strategy_run")

#: Rule B: relations that hold results addressed by a head. A reader of one must also read the
#: served head, or it serves a run that nobody checked for age.
RESULT_RELATIONS = (
    "mart.topt_core_results",
    "mart.topt_core_result_read",
    "mart.topt_gppe_results",
    "mart.strategy_decisions",
    "mart.fund_virtual_company",
    "mart.issuer_theme_purity",
    "mart.issuer_analyst_ratings",
    "mart.issuer_supply_chain_exposure",
)

SERVED_HEAD = "mart.served_head"

#: Completeness: `mart` relations with a `run_id` column that a consumer serves through the head.
#: `mart.strategy_decisions` has no `run_id` column (it carries `strategy_run_id`), so it is in
#: RESULT_RELATIONS only.
SERVED = (
    "mart.topt_core_results",
    "mart.topt_core_result_read",
    "mart.topt_gppe_results",
    "mart.fund_virtual_company",
    "mart.issuer_theme_purity",
    "mart.issuer_analyst_ratings",
    "mart.issuer_supply_chain_exposure",
)

#: Completeness: `mart` relations with a `run_id` column that no consumer serves as a head-addressed
#: result, each with the reason. A new relation with a `run_id` column is in neither list, and the
#: completeness test names it.
NOT_SERVED = {
    "mart.served_head": "the read point itself; it ages its own rows",
    "mart.data_engine_identity": "the build identity of the data engine; a deploy fact, not a served value",
    "mart.datahub_quality_report": "the grade of one run; read by the run id that the head gave, so it ages with the head",
    "mart.datahub_confidence_report": "the nightly operator report; its freshness is a nightly verdict",
    "mart.question_coverage_report": "the weekly operator report; its freshness is a nightly verdict",
    "mart.strategy_input_coverage": "the operator fix-list of one run; read by the run id that the head gave",
    "mart.topt_capture_status": "the status of one capture run; read by the run id that the head gave",
    "mart.topt_capture_meta_info": "the metadata of one capture run; read by the run id that the head gave",
    "mart.topt_core_meta_info": "the metadata of one core run; read by the run id that the head gave",
    "mart.backtest_runs": "backtest results keyed by a backtest run; no consumer in this repository reads them",
    "mart.backtest_trades": "backtest results keyed by a backtest run; no consumer in this repository reads them",
    "mart.backtest_valuations": "backtest results keyed by a backtest run; no consumer in this repository reads them",
}


# --- reading source: string literals and code, comments removed ----------------------------


@dataclass(frozen=True)
class Literal:
    """One string a program holds, with adjacent pieces joined. `${}` stands for an interpolation."""

    text: str
    line: int


@dataclass(frozen=True)
class Source:
    literals: list[Literal]
    #: The code with comments (and, for Python, docstrings) removed. Strings stay in it.
    code: str


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def read_typescript(text: str) -> Source:
    """Strings and code of a TypeScript or JavaScript file, without a parser.

    Handles `//` and `/* */` comments, quotes with escapes, template literals with nested
    `${}` expressions, and `"a" + "b"` concatenation. A quote or `//` inside a regular
    expression or JSX text can hide the rest of that one line. A single-quoted or
    double-quoted string never runs past its line, which bounds that damage.
    """
    count = len(text)
    code: list[str] = []
    spans: list[tuple[int, int, str]] = []
    stack: list[list] = [["code", 0]]  # ["code", brace depth] or ["template", start, parts]
    position = 0
    while position < count:
        context = stack[-1]
        char = text[position]
        if context[0] == "code":
            pair = text[position : position + 2]
            if pair == "//":
                end = text.find("\n", position)
                end = count if end < 0 else end
                code.append(" " * (end - position))
                position = end
            elif pair == "/*":
                end = text.find("*/", position + 2)
                end = count if end < 0 else end + 2
                code.append("".join(c if c == "\n" else " " for c in text[position:end]))
                position = end
            elif char in "'\"":
                end = position + 1
                parts: list[str] = []
                while end < count and text[end] != char and text[end] != "\n":
                    if text[end] == "\\" and end + 1 < count:
                        parts.append(text[end + 1])
                        end += 2
                    else:
                        parts.append(text[end])
                        end += 1
                if end < count and text[end] == char:
                    end += 1
                spans.append((position, end, "".join(parts)))
                code.append(text[position:end])
                position = end
            elif char == "`":
                stack.append(["template", position, []])
                code.append(char)
                position += 1
            elif char == "}" and context[1] == 0 and len(stack) > 1:
                stack.pop()
                code.append(char)
                position += 1
            else:
                if char == "{":
                    context[1] += 1
                elif char == "}":
                    context[1] -= 1
                code.append(char)
                position += 1
        else:
            if char == "\\" and position + 1 < count:
                context[2].append(text[position + 1])
                code.append(text[position : position + 2])
                position += 2
            elif char == "`":
                spans.append((context[1], position + 1, "".join(context[2])))
                stack.pop()
                code.append(char)
                position += 1
            elif text[position : position + 2] == "${":
                context[2].append("${}")
                stack.append(["code", 0])
                code.append("${")
                position += 2
            else:
                context[2].append(char)
                code.append(char)
                position += 1
    stripped = "".join(code)
    spans.sort()
    literals: list[Literal] = []
    previous_end: int | None = None
    for start, end, value in spans:
        if previous_end is not None and start < previous_end:
            # A template inside an `${}` of the one before: its own string.
            literals.append(Literal(value, _line_of(text, start)))
            continue
        # "a" + "b": the pieces are one statement.
        if previous_end is not None and re.fullmatch(r"\s*\+\s*", stripped[previous_end:start]):
            literals[-1] = Literal(literals[-1].text + value, literals[-1].line)
        else:
            literals.append(Literal(value, _line_of(text, start)))
        previous_end = end
    return Source(literals, stripped)


def _fold(node: ast.AST) -> str | None:
    """The text of a string expression, `${}` for a part that is not a constant."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value if isinstance(part, ast.Constant) and isinstance(part.value, str) else "${}"
            for part in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        pieces = [_fold(side) for side in _operands(node)]
        if all(piece is None for piece in pieces):
            return None
        return "".join("${}" if piece is None else piece for piece in pieces)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return _fold(node.left)
    return None


def _operands(node: ast.AST) -> list[ast.AST]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return [*_operands(node.left), *_operands(node.right)]
    return [node]


def read_python(text: str) -> Source:
    tree = ast.parse(text)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstrings.add(id(first.value))
    literals: list[Literal] = []

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.Constant | ast.JoinedStr | ast.BinOp) and id(node) not in docstrings:
            folded = _fold(node)
            if folded is not None:
                literals.append(Literal(folded, getattr(node, "lineno", 1)))
                return
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    docstring_positions = {
        (node.lineno, node.col_offset)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and id(node) in docstrings
    }
    pieces = [
        token.string
        for token in tokenize.generate_tokens(io.StringIO(text).readline)
        if token.type not in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT)
        and not (token.type == tokenize.STRING and token.start in docstring_positions)
    ]
    return Source(literals, " ".join(pieces))


# --- the rules ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Violation:
    rule: str
    file: str
    pattern: str
    line: int

    def key(self) -> tuple[str, str, str]:
        return (self.rule, self.file, self.pattern)

    def __str__(self) -> str:
        return f"{self.file}:{self.line} rule {self.rule} {self.pattern}"


def _without_sql_comments(statement: str) -> str:
    return re.sub(r"--[^\n]*|/\*.*?\*/", " ", statement, flags=re.DOTALL)


def is_sql(text: str) -> bool:
    lowered = text.lower()
    return bool(
        (re.search(r"\bselect\b", lowered) and re.search(r"\bfrom\b", lowered))
        or re.search(r"\b(insert\s+into|delete\s+from)\b", lowered)
        or (re.search(r"\bupdate\b", lowered) and re.search(r"\bset\b", lowered))
    )


def _relation(name: str) -> re.Pattern[str]:
    return re.compile(rf"\b{re.escape(name)}\b")


#: Rule C, in SQL statements.
STATEMENT_AGE_PATTERNS = {
    "now-minus-advanced": re.compile(
        r"(?:\bnow\(\)|\bcurrent_timestamp\b|\bclock_timestamp\(\))\s*-\s*[(\w.\"]*advanced", re.IGNORECASE
    ),
    "advanced-compared-to-now": re.compile(
        r"advanced\w*\s*[<>]=?\s*\(?\s*(?:now\(\)|current_timestamp)", re.IGNORECASE
    ),
    "extract-epoch-from-now": re.compile(r"extract\s*\(\s*epoch\s+from\s*\(\s*now\(\)", re.IGNORECASE),
    "age-of-advanced": re.compile(r"\bage\s*\([^)]*advanced", re.IGNORECASE),
}
STALE_AFTER = re.compile(r"\bSTALE_AFTER\w*")
CLOCK_CALL = re.compile(r"Date\.now\(\)|new Date\(\)|datetime\.now\(|datetime\.utcnow\(|time\.time\(\)")
ADVANCED_NAME = re.compile(r"\badvanced\w*")
BARE_POINTER_NAME = re.compile(
    r"\s*[\"'`]?(" + "|".join(re.escape(name) for name in POINTER_RELATIONS) + r")[\"'`]?\s*"
)


def violations_of(relative: str, source: Source) -> list[Violation]:
    found: list[Violation] = []
    statements = [
        Literal(_without_sql_comments(literal.text), literal.line)
        for literal in source.literals
        if is_sql(literal.text)
    ]
    # Rule A. A string that is only a pointer relation's name is a constant that a statement can
    # interpolate, so it counts as a reader too.
    for name in POINTER_RELATIONS:
        pattern = _relation(name)
        for statement in statements:
            found += [Violation("A", relative, name, statement.line) for _ in pattern.finditer(statement.text)]
    for literal in source.literals:
        bare = BARE_POINTER_NAME.fullmatch(literal.text)
        if bare:
            found.append(Violation("A", relative, bare.group(1), literal.line))
    # Rule B.
    reads_served_head = any(_relation(SERVED_HEAD).search(statement.text) for statement in statements)
    if not reads_served_head:
        for name in RESULT_RELATIONS:
            pattern = _relation(name)
            lines = [statement.line for statement in statements if pattern.search(statement.text)]
            if lines:
                found.append(Violation("B", relative, name, lines[0]))
    # Rule C.
    for label, pattern in STATEMENT_AGE_PATTERNS.items():
        for statement in statements:
            found += [Violation("C", relative, label, statement.line) for _ in pattern.finditer(statement.text)]
    found += [Violation("C", relative, "stale-after-constant", 1) for _ in STALE_AFTER.finditer(source.code)]
    if CLOCK_CALL.search(source.code) and ADVANCED_NAME.search(source.code):
        found.append(Violation("C", relative, "clock-with-advanced", 1))
    return found


def consumer_files(root: Path) -> Iterator[Path]:
    for consumer_root in CONSUMER_ROOTS:
        base = root / consumer_root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and not (set(path.relative_to(root).parts) & SKIP_DIRECTORIES):
                if path.suffix == ".py" or path.suffix in TS_SUFFIXES:
                    yield path


def read_file(path: Path) -> Source:
    text = path.read_text(encoding="utf-8")
    return read_python(text) if path.suffix == ".py" else read_typescript(text)


def scan(root: Path = REPO_ROOT) -> tuple[list[Violation], int]:
    """Every violation under the consumer trees of `root`, and the number of files read."""
    violations: list[Violation] = []
    files = 0
    for path in consumer_files(root):
        files += 1
        violations += violations_of(path.relative_to(root).as_posix(), read_file(path))
    return violations, files


# --- the baseline ------------------------------------------------------------------------


@dataclass(frozen=True)
class Offender:
    rule: str
    file: str
    pattern: str
    count: int
    #: The follow-up change that moves this reader to `mart.served_head`:
    #: 2 = the Python readers in libs/contracts, 3 = the Web App research readers,
    #: 4 = the Web App admin readers.
    pr: int


#: The readers that exist at the base of #1062, found by `scan()` on that tree. Each is moved by a
#: follow-up change, which deletes its entries here. `llm_service/main.py` is fixed in #1062 itself.
BASELINE: tuple[Offender, ...] = (
    Offender("A", "libs/contracts/src/truealpha_contracts/topt_read.py", "mart.current_pointer_head", 1, 2),
    Offender("B", "libs/contracts/src/truealpha_contracts/topt_read.py", "mart.topt_gppe_results", 1, 2),
    Offender(
        "A", "libs/contracts/src/truealpha_contracts/strategy_run_postgres.py", "mart.governed_strategy_run", 1, 2
    ),
    Offender("B", "libs/contracts/src/truealpha_contracts/strategy_run_postgres.py", "mart.strategy_decisions", 1, 2),
    Offender("B", "libs/contracts/src/truealpha_contracts/strategy_run_postgres.py", "mart.topt_core_results", 1, 2),
    Offender("A", "apps/app-web/src/server/mart/topt-gppe-repository.ts", "mart.current_pointer_head", 1, 3),
    Offender("B", "apps/app-web/src/server/mart/topt-gppe-repository.ts", "mart.topt_gppe_results", 1, 3),
    Offender("A", "apps/app-web/src/server/mart/fund-valuation.ts", "mart.current_pointer_head", 1, 3),
    Offender("B", "apps/app-web/src/server/mart/fund-valuation.ts", "mart.fund_virtual_company", 1, 3),
    Offender("B", "apps/app-web/src/server/mart/fund-valuation.ts", "mart.topt_core_result_read", 1, 3),
    Offender("A", "apps/app-web/src/server/mart/strategy-run-repository.ts", "mart.governed_strategy_run", 1, 3),
    Offender("B", "apps/app-web/src/server/mart/strategy-run-repository.ts", "mart.strategy_decisions", 1, 3),
    Offender("B", "apps/app-web/src/server/mart/strategy-run-repository.ts", "mart.topt_core_results", 1, 3),
    Offender("B", "apps/app-web/src/server/mart/theme-purity.ts", "mart.issuer_theme_purity", 1, 3),
    Offender("C", "apps/app-web/src/app/research/served-run-age.ts", "stale-after-constant", 2, 3),
    Offender("A", "apps/app-web/src/server/admin/ops.ts", "mart.current_pointer_head", 1, 4),
    Offender("A", "apps/app-web/src/server/admin/funnel.ts", "mart.current_pointer_head", 1, 4),
    Offender("C", "apps/app-web/src/server/admin/funnel.ts", "extract-epoch-from-now", 1, 4),
    Offender("C", "apps/app-web/src/server/admin/funnel.ts", "now-minus-advanced", 1, 4),
    Offender("A", "apps/app-web/src/server/admin/datahub-stats.ts", "mart.current_pointer_head", 1, 4),
    Offender("C", "apps/app-web/src/app/admin/page.tsx", "clock-with-advanced", 1, 4),
)


def counted(violations: list[Violation]) -> Counter[tuple[str, str, str]]:
    return Counter(violation.key() for violation in violations)


def ratchet(violations: list[Violation], baseline: tuple[Offender, ...]) -> tuple[list[str], list[str]]:
    """(violations beyond the baseline, baseline entries that no longer match exactly)."""
    actual = counted(violations)
    allowed = {(entry.rule, entry.file, entry.pattern): entry.count for entry in baseline}
    new = [f"{violation}" for violation in violations if actual[violation.key()] > allowed.get(violation.key(), 0)]
    gone = [
        f"{entry.file} rule {entry.rule} {entry.pattern}: baseline says {entry.count}, found {actual[(entry.rule, entry.file, entry.pattern)]}"
        for entry in baseline
        if actual[(entry.rule, entry.file, entry.pattern)] != entry.count
    ]
    return sorted(set(new)), gone


def test_no_consumer_imports_the_data_engine() -> None:
    """The allowlist (data-engine and tools may read the pointer) is sound only if no consumer
    reaches them."""
    importers: list[str] = []
    for path in consumer_files(REPO_ROOT):
        if path.suffix != ".py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            if any(name == "data_engine" or name.startswith("data_engine.") for name in names):
                importers.append(path.relative_to(REPO_ROOT).as_posix())
    assert importers == []


def test_the_scan_reads_the_consumer_trees_and_finds_statements() -> None:
    """Green while empty: a scanner that reads nothing passes every rule."""
    violations, files = scan()
    assert files > 100, f"the scan read only {files} files"
    served = [
        literal
        for path in consumer_files(REPO_ROOT)
        for literal in read_file(path).literals
        if is_sql(literal.text) and SERVED_HEAD in literal.text
    ]
    assert served, "no consumer statement reads mart.served_head, so the guard has seen no compliant reader"
    assert violations, "the scan found no violation at all, so the rules cannot be reading the baseline readers"


def test_no_consumer_reads_the_pointer_or_computes_an_age_beyond_the_baseline() -> None:
    violations, _ = scan()
    new, _ = ratchet(violations, BASELINE)
    assert new == [], (
        "consumer-reachable code reads a head with no age, or computes a head age itself. "
        "Read `mart.served_head` and take age, limit, freshness and availability from it (#1062): " + "; ".join(new)
    )


def test_every_baseline_entry_still_violates_so_the_list_only_shrinks() -> None:
    violations, _ = scan()
    _, gone = ratchet(violations, BASELINE)
    assert gone == [], (
        "these readers changed. Delete the baseline entry, or lower its count, in the same change: " + "; ".join(gone)
    )


def test_the_baseline_is_well_formed() -> None:
    keys = [(entry.rule, entry.file, entry.pattern) for entry in BASELINE]
    assert len(keys) == len(set(keys)), "a baseline entry is listed twice"
    for entry in BASELINE:
        assert entry.rule in {"A", "B", "C"}, entry
        assert entry.pr in {2, 3, 4}, f"{entry} names no follow-up change"
        assert entry.count >= 1, entry
        assert (REPO_ROOT / entry.file).is_file(), f"{entry.file} is gone: delete its entries"
        assert entry.file.startswith(CONSUMER_ROOTS), f"{entry.file} is outside the consumer trees"
        if entry.rule == "A":
            assert entry.pattern in POINTER_RELATIONS, entry
        if entry.rule == "B":
            assert entry.pattern in RESULT_RELATIONS, entry
        if entry.rule == "C":
            assert entry.pattern in {*STATEMENT_AGE_PATTERNS, "stale-after-constant", "clock-with-advanced"}, entry


def test_the_health_endpoint_reads_the_served_head() -> None:
    """Fixed in #1062 itself, so it is not in the baseline."""
    relative = "apps/llm-service/src/llm_service/main.py"
    violations = violations_of(relative, read_file(REPO_ROOT / relative))
    assert violations == []
    statements = [literal for literal in read_file(REPO_ROOT / relative).literals if is_sql(literal.text)]
    assert any(SERVED_HEAD in statement.text for statement in statements)
    assert not any(entry.file == relative for entry in BASELINE)


# --- the rules go red on the readers they exist for ----------------------------------------


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _found(root: Path) -> set[tuple[str, str, str]]:
    violations, _ = scan(root)
    return {violation.key() for violation in violations}


def test_a_typescript_reader_that_bypasses_the_served_head_is_reported(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "apps/app-web/src/server/mart/bypass.ts",
        "const stamp = '// not a comment';\n"
        "export const HEAD_SQL = `\n"
        "  select target_run_id as run_id from mart.current_pointer_head\n"
        "  where extract(epoch from (now() - advanced_at)) / 3600 < 36\n"
        "`;\n"
        "export const RESULTS_SQL = `select * from mart.topt_gppe_results where run_id = $1`;\n"
        "export const STALE_AFTER_HOURS = 36;\n"
        "export const age = (advancedAt: string) => Date.now() - new Date(advancedAt).getTime();\n",
    )
    file = "apps/app-web/src/server/mart/bypass.ts"
    assert _found(tmp_path) == {
        ("A", file, "mart.current_pointer_head"),
        ("B", file, "mart.topt_gppe_results"),
        ("C", file, "now-minus-advanced"),
        ("C", file, "extract-epoch-from-now"),
        ("C", file, "stale-after-constant"),
        ("C", file, "clock-with-advanced"),
    }


def test_a_python_reader_that_bypasses_the_served_head_is_reported(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "libs/contracts/src/truealpha_contracts/bypass.py",
        '"""Docstring: select the run from mart.current_pointer_head. Not a statement."""\n'
        "HEAD = (\n"
        '    "select target_run_id from mart.current_pointer_head "\n'
        "    \"where now() - advanced_at < interval '3 days'\"\n"
        ")\n"
        'GOVERNED = "mart.governed_strategy_run"\n'
        'RESULTS = """\n'
        "    select * from mart.strategy_decisions  -- mart.issuer_theme_purity is only named here\n"
        '"""\n',
    )
    file = "libs/contracts/src/truealpha_contracts/bypass.py"
    found = _found(tmp_path)
    assert ("A", file, "mart.current_pointer_head") in found
    assert ("A", file, "mart.governed_strategy_run") in found, "a constant that names a pointer relation is a reader"
    assert ("B", file, "mart.strategy_decisions") in found
    assert ("C", file, "now-minus-advanced") in found
    assert ("B", file, "mart.issuer_theme_purity") not in found, "a SQL comment names a relation; it does not read it"


def test_a_reader_of_the_served_head_is_clean(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "apps/app-web/src/server/mart/clean.ts",
        "// mart.current_pointer_head is the old read point; this file does not use it\n"
        "export const SQL = `\n"
        "  select h.run_id, h.freshness, h.availability, h.age_hours\n"
        "  from mart.served_head h\n"
        "  join mart.topt_gppe_results r on r.run_id = h.run_id\n"
        "`;\n",
    )
    _write(
        tmp_path,
        "libs/contracts/src/truealpha_contracts/clean.py",
        '"""Reads mart.current_pointer_head? No."""\n'
        "SQL = 'select run_id, freshness from mart.served_head'\n"
        "# STALE_AFTER = 36 is a comment\n",
    )
    assert _found(tmp_path) == set()


def test_a_statement_split_over_pieces_is_one_statement(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "apps/app-web/src/server/admin/split.ts",
        'export const SQL = "select universe_id, advanced_at " +\n'
        '  "from mart.current_pointer_head " +\n'
        '  "order by universe_id";\n',
    )
    assert _found(tmp_path) == {("A", "apps/app-web/src/server/admin/split.ts", "mart.current_pointer_head")}


def test_a_template_with_an_interpolation_is_read_whole(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "apps/app-web/src/server/admin/interpolated.ts",
        "export const f = (n: number) => `select ${cols(`x${n}`)} from mart.current_pointer_head limit ${n}`;\n"
        "export const g = `select 1 from mart.served_head`;\n",
    )
    assert _found(tmp_path) == {("A", "apps/app-web/src/server/admin/interpolated.ts", "mart.current_pointer_head")}


def test_the_ratchet_reports_a_new_violation_and_a_stale_entry() -> None:
    reader = Violation("A", "apps/app-web/src/server/mart/a.ts", "mart.current_pointer_head", 7)
    entry = Offender("A", reader.file, reader.pattern, 1, 3)
    assert ratchet([reader], (entry,)) == ([], [])
    new, gone = ratchet([reader], ())
    assert new == [str(reader)] and gone == []
    new, gone = ratchet([], (entry,))
    assert new == [] and len(gone) == 1, "a fixed reader whose entry stays must fail"
    # A second statement in a baselined file is a new violation, not covered by the old entry.
    second = Violation("A", reader.file, reader.pattern, 40)
    new, _ = ratchet([reader, second], (entry,))
    assert new, "a second statement in a baselined file must fail"
    # A different relation in the same file is new too.
    other = Violation("A", reader.file, "mart.current_pointer", 9)
    assert ratchet([reader, other], (entry,))[0] == [str(other)]


# --- completeness: every mart relation with a run_id is served or explained ---------------


def test_the_served_and_not_served_lists_are_disjoint_and_explained() -> None:
    assert set(SERVED).isdisjoint(NOT_SERVED)
    assert set(SERVED) <= set(RESULT_RELATIONS)
    assert all(len(reason) >= 20 for reason in NOT_SERVED.values()), "every NOT_SERVED entry needs a real reason"


def _named(database: str) -> str:
    base = urlsplit(os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/truealpha"))
    return urlunsplit((base.scheme, base.netloc, f"/{database}", base.query, ""))


@pytest.fixture(scope="module")
def run_id_relations() -> Iterator[set[str]]:
    """Every `mart` table and view with a `run_id` column, on a scratch migrated database."""
    name = f"truealpha_served_guard_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    try:
        with psycopg.connect(_named("postgres"), connect_timeout=3, autocommit=True) as admin:
            admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        skip_or_fail(f"no local Postgres; CI runs the required integration coverage ({error})")
    try:
        apply_migration_chain(_named(name))
        with psycopg.connect(_named(name)) as connection:
            rows = connection.execute(
                """
                select 'mart.' || c.table_name
                from information_schema.columns c
                join information_schema.tables t using (table_schema, table_name)
                where c.table_schema = 'mart' and c.column_name = 'run_id'
                  and t.table_type in ('BASE TABLE', 'VIEW')
                """
            ).fetchall()
        yield {row[0] for row in rows}
    finally:
        with psycopg.connect(_named("postgres"), autocommit=True) as admin:
            admin.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name)))


def unclassified(relations: set[str], served: tuple[str, ...], not_served: dict[str, str]) -> list[str]:
    return sorted(relations - set(served) - set(not_served))


def test_every_mart_relation_with_a_run_id_is_served_or_explained(run_id_relations: set[str]) -> None:
    assert len(run_id_relations) >= 15, f"the catalog read found only {sorted(run_id_relations)}"
    assert unclassified(run_id_relations, SERVED, NOT_SERVED) == [], (
        "a mart relation with a run_id column is in neither SERVED nor NOT_SERVED. A consumer that reads it "
        "serves a run: put it in SERVED, and read it through mart.served_head, or name why not in NOT_SERVED"
    )


def test_no_listed_relation_is_missing_from_the_database(run_id_relations: set[str]) -> None:
    """A list entry for a relation that is gone would outlive its defect."""
    assert sorted((set(SERVED) | set(NOT_SERVED)) - run_id_relations) == []


def test_a_new_relation_with_a_run_id_is_reported() -> None:
    assert unclassified({*SERVED, "mart.brand_new_results"}, SERVED, NOT_SERVED) == ["mart.brand_new_results"]
