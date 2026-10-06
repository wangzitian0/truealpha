"""A consumer reads the served head, and nothing else computes a head age (#1062).

`mart.served_head` is the one read point. It carries the age, the limit, the label and the
availability of every governed head. A consumer that reads a pointer relation gets a head with
no age. A consumer that subtracts a clock from `advanced_at` makes a second definition of age.

The scope is what the guard must govern, not what passes today. It reads every file under the
three consumer-reachable trees. Three rules run without a database:

  A  no SQL text reads a pointer relation. It reads `mart.served_head`.
  B  a file that reads a run-addressed relation also has SQL text on `mart.served_head`.
  C  no file computes a head age: `now() - advanced_at`, `STALE_AFTER`, or a clock next to
     an `advanced*` name.

Rule B works per file, and a static scan cannot do better. The scan cannot prove that a run id
flows from the head into a result query. A file that reads the head in one statement and the
newest run in another passes rule B. Rule B finds files that never read the head. Review finds
the rest. The follow-up changes of #1062 must check each reader by hand.

A fourth rule runs on a scratch migrated database. Every `mart` relation with a run key must be
in SERVED or NOT_SERVED. A relation in NOT_SERVED must not appear in any consumer file.

The guard is a ratchet. BASELINE lists the readers that exist today, each with its follow-up
change. A violation outside the baseline fails. A baseline entry that no longer matches fails
too, so the list only shrinks. The match uses the rule, the file and the pattern. It does not
use a line number, so an edit above a statement does not break it.
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
#: `apps/data-engine/src` and `tools/` are not consumers. They may read the pointer. That holds
#: only while no consumer imports them. `test_no_consumer_imports_the_data_engine` checks it.
CONSUMER_ROOTS = ("apps/app-web/src", "apps/llm-service/src", "libs/contracts/src")

TS_SUFFIXES = {".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"}
SKIP_DIRECTORIES = {"node_modules", ".next", "__pycache__", "dist", "build"}

#: Rule A: relations that hold a head with no age.
POINTER_RELATIONS = ("mart.current_pointer_head", "mart.current_pointer", "mart.governed_strategy_run")

#: The one read point, and the view of environments beside it. The second is not a head.
SERVED_HEAD = "mart.served_head"
HEAD_ENVIRONMENTS = "mart.served_head_environments"

#: Columns that address a run, or the artifacts of one run. A mart relation with one of them
#: holds results that a head must select.
RUN_KEY_COLUMNS = ("run_id", "target_run_id", "capture_run_id", "strategy_run_id", "snapshot_id", "invocation_id")

#: Columns that look like a run key and are not one. A new column in `mart` with `run`,
#: `snapshot_id` or `invocation_id` in its name must be in RUN_KEY_COLUMNS or here.
NOT_RUN_KEYS = {
    "previous_run_id": "the earlier run of a pointer; pointer relations are rule A",
    "head_run_id": "the run column that mart.served_head itself exposes",
    "dagster_run_id": "a Dagster run that wrote a verdict, not a data run",
    "gppe_invocation_id": "a second key beside invocation_id on a table that has both",
}

#: Rule B and completeness. Relations with a run key that a consumer-reachable file reads. A
#: consumer must select their rows through the head's `run_id`. A read of the newest run by
#: `order by cutoff desc limit 1` is the defect that #1062 exists for.
SERVED = (
    "mart.topt_core_results",
    "mart.topt_core_result_read",
    "mart.topt_gppe_results",
    "mart.strategy_decisions",
    "mart.strategy_runs",
    "mart.strategy_run_capture",
    "mart.fund_virtual_company",
    "mart.issuer_theme_purity",
    "mart.issuer_analyst_ratings",
    "mart.issuer_supply_chain_exposure",
    "mart.datahub_quality_report",
    "mart.datahub_confidence_report",
    "mart.question_coverage_report",
    "mart.strategy_input_coverage",
    "mart.topt_capture_status",
    "mart.data_engine_identity",
)

#: Relations with a run key that no consumer-reachable file reads. The reason is checkable:
#: `test_no_consumer_reaches_a_relation_listed_as_not_served` fails when a file names one.
NOT_SERVED = {
    "mart.backtest_runs": "backtest results keyed by a backtest run; no consumer reads them",
    "mart.backtest_trades": "backtest results keyed by a backtest run; no consumer reads them",
    "mart.backtest_valuations": "backtest results keyed by a backtest run; no consumer reads them",
    "mart.topt_capture_meta_info": "metadata of one capture run; no consumer reads it",
    "mart.topt_core_meta_info": "metadata of one core run; no consumer reads it",
    "mart.topt_core_invocations": "factor invocation records; no consumer reads them",
    "mart.topt_gppe_invocations": "factor invocation records; no consumer reads them",
    "mart.strategy_run_capture_bindings": "the write side of the capture binding; no consumer reads it",
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
    #: Lines where code subtracts or compares a clock against an `advanced*` name.
    head_age_lines: list[int]


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


#: A `/` after one of these characters, or after one of these words, starts a regular expression.
_REGEX_AFTER_CHARACTER = "(,=:[!&|?{;+-*%<>~^"
_REGEX_AFTER_WORD = re.compile(r"\b(?:return|typeof|case|in|of|delete|void|throw|else|do|yield|await)$")


def _starts_a_regular_expression(code_so_far: str) -> bool:
    before = code_so_far.rstrip()
    if not before:
        return True
    if before[-1] == "<":  # `</div>` in JSX closes a tag
        return False
    return before[-1] in _REGEX_AFTER_CHARACTER or bool(_REGEX_AFTER_WORD.search(before))


def read_typescript(text: str) -> Source:
    """Strings and code of a TypeScript or JavaScript file, without a parser.

    It handles `//` and `/* */` comments and quotes with escapes. It handles regular expression
    literals and template literals with nested `${}` expressions. It joins `"a" + "b"` pieces.
    JSX text with an apostrophe or `//` can hide the rest of that one line. A quoted string never
    runs past its line, which bounds that damage.
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
            elif char == "/" and _starts_a_regular_expression("".join(code)):
                end = position + 1
                in_class = False
                closed = False
                while end < count and text[end] != "\n":
                    if text[end] == "\\":
                        end += 2
                        continue
                    if text[end] == "[":
                        in_class = True
                    elif text[end] == "]":
                        in_class = False
                    elif text[end] == "/" and not in_class:
                        end += 1
                        closed = True
                        break
                    end += 1
                if closed:
                    code.append(text[position:end])
                    position = end
                else:
                    code.append(char)
                    position += 1
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
    age_lines = [1] if TS_CLOCK_CALL.search(stripped) and ADVANCED_NAME.search(stripped) else []
    return Source(literals, stripped, age_lines)


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


_CLOCK_METHODS = {"now", "utcnow", "today"}
_CLOCK_VARIABLES = {"now", "utcnow", "as_of"}


def _holds_a_clock(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            function = child.func
            name = function.attr if isinstance(function, ast.Attribute) else getattr(function, "id", "")
            module = (
                function.value.id
                if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name)
                else ""
            )
            if name in _CLOCK_METHODS or (name == "time" and module == "time"):
                return True
        if isinstance(child, ast.Name) and child.id in _CLOCK_VARIABLES:
            return True
    return False


def _holds_an_advanced_name(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id.startswith("advanced"):
            return True
        if isinstance(child, ast.Attribute) and child.attr.startswith("advanced"):
            return True
        if isinstance(child, ast.Constant) and isinstance(child.value, str) and child.value.startswith("advanced"):
            return True
    return False


def _python_head_age_lines(tree: ast.AST) -> list[int]:
    """Lines where a clock and an `advanced*` name meet in a subtraction or a comparison.

    Both `datetime.now(UTC) - head.advanced_at` and `advanced_at < now - timedelta(days=3)` count.
    The check reads the syntax tree, so spaces and line breaks do not matter.
    """
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub):
            operands = [node.left, node.right]
        elif isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
        else:
            continue
        if any(
            _holds_a_clock(first) and _holds_an_advanced_name(second)
            for first in operands
            for second in operands
            if first is not second
        ):
            lines.append(getattr(node, "lineno", 1))
    return lines


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
    return Source(literals, " ".join(pieces), _python_head_age_lines(tree))


# --- reading SQL text -----------------------------------------------------------------------


def normalise(text: str) -> str:
    """Case-fold, drop identifier quotes and close the spaces around dots.

    `"MART" . "Current_Pointer_Head"` becomes `mart.current_pointer_head`.
    """
    folded = text.casefold().replace('"', "").replace("`", "")
    return re.sub(r"\s*\.\s*", ".", folded)


def _without_sql_comments(text: str) -> str:
    return re.sub(r"--[^\n]*|/\*.*?\*/", " ", text, flags=re.DOTALL)


def is_sql(text: str) -> bool:
    lowered = text.lower()
    return bool(
        (re.search(r"\bselect\b", lowered) and re.search(r"\bfrom\b", lowered))
        or re.search(r"\b(insert\s+into|delete\s+from)\b", lowered)
        or (re.search(r"\bupdate\b", lowered) and re.search(r"\bset\b", lowered))
    )


def relation_references(text: str, relation: str) -> int:
    """How many times SQL text reads `relation`, after normalising the text.

    A schema-qualified name counts in any SQL statement. In a fragment with no `select`, a name
    counts after `from`, `join`, `update` or `into`. An unqualified name counts there too. The
    schema may be `mart` or an interpolation: `${}.current_pointer_head`.
    """
    short = relation.split(".", 1)[1]
    cleaned = normalise(_without_sql_comments(text))
    qualified = rf"(?:\bmart|\$\{{\}})\.{re.escape(short)}\b"
    after_keyword = rf"\b(?:from|join|update|into)\s*\(?\s*(?:only\s+)?(?:(?:mart|\$\{{\}})\.)?{re.escape(short)}\b"
    ends = {match.end() for match in re.finditer(after_keyword, cleaned)}
    if is_sql(cleaned):
        ends |= {match.end() for match in re.finditer(qualified, cleaned)}
    return len(ends)


def _bare_relation(text: str, relations: tuple[str, ...]) -> str | None:
    """The relation, when a string is only that relation's qualified name."""
    names = "|".join(re.escape(relation.split(".", 1)[1]) for relation in relations)
    match = re.fullmatch(rf"\s*(?:mart\.|\$\{{\}}\.)({names})\s*", normalise(text))
    return f"mart.{match.group(1)}" if match else None


#: Rule C, in SQL text.
SQL_AGE_PATTERNS = {
    "now-minus-advanced": re.compile(
        r"(?:\bnow\(\)|\bcurrent_timestamp\b|\bclock_timestamp\(\))\s*-\s*[(\w.]*advanced"
    ),
    "advanced-compared-to-now": re.compile(r"advanced\w*\s*[<>]=?\s*\(?\s*(?:now\(\)|current_timestamp)"),
    "extract-epoch-from-now": re.compile(r"extract\s*\(\s*epoch\s+from\s*\(\s*now\(\)"),
    "age-of-advanced": re.compile(r"\bage\s*\([^)]*advanced"),
}
STALE_AFTER = re.compile(r"\bstale_after\w*")
TS_CLOCK_CALL = re.compile(r"Date\.now\(\)|new Date\(\)")
ADVANCED_NAME = re.compile(r"\badvanced\w*")


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


def violations_of(relative: str, source: Source) -> list[Violation]:
    found: list[Violation] = []
    # Rule A. A string can be only a pointer relation's name. A statement can interpolate such a
    # constant. So it counts as a reader too.
    for relation in POINTER_RELATIONS:
        for literal in source.literals:
            found += [
                Violation("A", relative, relation, literal.line)
                for _ in range(relation_references(literal.text, relation))
            ]
    for literal in source.literals:
        bare = _bare_relation(literal.text, POINTER_RELATIONS)
        if bare:
            found.append(Violation("A", relative, bare, literal.line))
    # Rule B. File level: see the module docstring for what it cannot prove.
    if not any(relation_references(literal.text, SERVED_HEAD) for literal in source.literals):
        for relation in SERVED:
            lines = [literal.line for literal in source.literals if relation_references(literal.text, relation)]
            if lines:
                found.append(Violation("B", relative, relation, lines[0]))
    # Rule C.
    for literal in source.literals:
        text = normalise(_without_sql_comments(literal.text))
        for label, pattern in SQL_AGE_PATTERNS.items():
            found += [Violation("C", relative, label, literal.line) for _ in pattern.finditer(text)]
    found += [Violation("C", relative, "stale-after-constant", 1) for _ in STALE_AFTER.finditer(source.code.casefold())]
    found += [Violation("C", relative, "clock-with-advanced", line) for line in source.head_age_lines]
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


def consumer_references(root: Path, relations: Iterator[str] | tuple[str, ...] | list[str]) -> dict[str, list[str]]:
    """Consumer files that name any of `relations`, in SQL text or anywhere in the code.

    Comments and Python docstrings do not count. A name in a string or in code does.
    """
    wanted = list(relations)
    found: dict[str, list[str]] = {}
    for path in consumer_files(root):
        source = read_file(path)
        code = normalise(source.code)
        named = [
            relation
            for relation in wanted
            if re.search(rf"\bmart\.{re.escape(relation.split('.', 1)[1])}\b", code)
            or any(relation_references(literal.text, relation) for literal in source.literals)
        ]
        if named:
            found[path.relative_to(root).as_posix()] = named
    return found


# --- the baseline ------------------------------------------------------------------------


@dataclass(frozen=True)
class Offender:
    rule: str
    file: str
    pattern: str
    count: int
    #: The follow-up change that moves this reader to `mart.served_head`.
    #: "#1062 PR 2" moves the Python readers in libs/contracts.
    #: "#1062 PR 3" moves the Web App research readers.
    #: "#1062 PR 4" moves the Web App admin readers.
    follow_up: str


FOLLOW_UP = re.compile(r"#1062 PR [234]")

#: The readers that exist at the base of #1062, found by `scan()` on that tree. A follow-up
#: change moves each one and deletes its entries here. `llm_service/main.py` is fixed in #1062
#: itself, so it is not here.
#:
#: Rule B is per file. `datahub-stats.ts` and `funnel.ts` read the newest graded run with
#: `order by cutoff desc limit 1`, with no head. When PR 4 moves their head statement, rule B
#: stops flagging those files. PR 4 must still move the newest-run statements by hand.
BASELINE: tuple[Offender, ...] = (
    Offender(
        "A",
        "libs/contracts/src/truealpha_contracts/strategy_run_postgres.py",
        "mart.governed_strategy_run",
        1,
        "#1062 PR 2",
    ),
    Offender(
        "B",
        "libs/contracts/src/truealpha_contracts/strategy_run_postgres.py",
        "mart.strategy_decisions",
        1,
        "#1062 PR 2",
    ),
    Offender(
        "B",
        "libs/contracts/src/truealpha_contracts/strategy_run_postgres.py",
        "mart.strategy_run_capture",
        1,
        "#1062 PR 2",
    ),
    Offender(
        "B", "libs/contracts/src/truealpha_contracts/strategy_run_postgres.py", "mart.strategy_runs", 1, "#1062 PR 2"
    ),
    Offender(
        "B",
        "libs/contracts/src/truealpha_contracts/strategy_run_postgres.py",
        "mart.topt_core_results",
        1,
        "#1062 PR 2",
    ),
    Offender("A", "libs/contracts/src/truealpha_contracts/topt_read.py", "mart.current_pointer_head", 1, "#1062 PR 2"),
    Offender(
        "B", "libs/contracts/src/truealpha_contracts/topt_read.py", "mart.datahub_quality_report", 1, "#1062 PR 2"
    ),
    Offender("B", "libs/contracts/src/truealpha_contracts/topt_read.py", "mart.topt_capture_status", 1, "#1062 PR 2"),
    Offender("B", "libs/contracts/src/truealpha_contracts/topt_read.py", "mart.topt_gppe_results", 1, "#1062 PR 2"),
    Offender("C", "apps/app-web/src/app/research/served-run-age.ts", "stale-after-constant", 2, "#1062 PR 3"),
    Offender("A", "apps/app-web/src/server/mart/fund-valuation.ts", "mart.current_pointer_head", 1, "#1062 PR 3"),
    Offender("B", "apps/app-web/src/server/mart/fund-valuation.ts", "mart.datahub_quality_report", 1, "#1062 PR 3"),
    Offender("B", "apps/app-web/src/server/mart/fund-valuation.ts", "mart.fund_virtual_company", 1, "#1062 PR 3"),
    Offender("B", "apps/app-web/src/server/mart/fund-valuation.ts", "mart.topt_capture_status", 1, "#1062 PR 3"),
    Offender("B", "apps/app-web/src/server/mart/fund-valuation.ts", "mart.topt_core_result_read", 1, "#1062 PR 3"),
    Offender(
        "A", "apps/app-web/src/server/mart/strategy-run-repository.ts", "mart.governed_strategy_run", 1, "#1062 PR 3"
    ),
    Offender(
        "B", "apps/app-web/src/server/mart/strategy-run-repository.ts", "mart.strategy_decisions", 1, "#1062 PR 3"
    ),
    Offender(
        "B", "apps/app-web/src/server/mart/strategy-run-repository.ts", "mart.strategy_run_capture", 1, "#1062 PR 3"
    ),
    Offender("B", "apps/app-web/src/server/mart/strategy-run-repository.ts", "mart.strategy_runs", 1, "#1062 PR 3"),
    Offender("B", "apps/app-web/src/server/mart/strategy-run-repository.ts", "mart.topt_core_results", 1, "#1062 PR 3"),
    Offender("B", "apps/app-web/src/server/mart/theme-purity.ts", "mart.issuer_theme_purity", 1, "#1062 PR 3"),
    Offender("A", "apps/app-web/src/server/mart/topt-gppe-repository.ts", "mart.current_pointer_head", 1, "#1062 PR 3"),
    Offender(
        "B", "apps/app-web/src/server/mart/topt-gppe-repository.ts", "mart.datahub_quality_report", 1, "#1062 PR 3"
    ),
    Offender("B", "apps/app-web/src/server/mart/topt-gppe-repository.ts", "mart.topt_capture_status", 1, "#1062 PR 3"),
    Offender("B", "apps/app-web/src/server/mart/topt-gppe-repository.ts", "mart.topt_gppe_results", 1, "#1062 PR 3"),
    Offender("C", "apps/app-web/src/app/admin/page.tsx", "clock-with-advanced", 1, "#1062 PR 4"),
    Offender("A", "apps/app-web/src/server/admin/datahub-stats.ts", "mart.current_pointer_head", 1, "#1062 PR 4"),
    Offender("B", "apps/app-web/src/server/admin/datahub-stats.ts", "mart.datahub_confidence_report", 1, "#1062 PR 4"),
    Offender("B", "apps/app-web/src/server/admin/datahub-stats.ts", "mart.datahub_quality_report", 1, "#1062 PR 4"),
    Offender("B", "apps/app-web/src/server/admin/datahub-stats.ts", "mart.question_coverage_report", 1, "#1062 PR 4"),
    Offender("B", "apps/app-web/src/server/admin/datahub-stats.ts", "mart.topt_capture_status", 1, "#1062 PR 4"),
    Offender("A", "apps/app-web/src/server/admin/funnel.ts", "mart.current_pointer_head", 1, "#1062 PR 4"),
    Offender("B", "apps/app-web/src/server/admin/funnel.ts", "mart.strategy_input_coverage", 1, "#1062 PR 4"),
    Offender("C", "apps/app-web/src/server/admin/funnel.ts", "extract-epoch-from-now", 1, "#1062 PR 4"),
    Offender("C", "apps/app-web/src/server/admin/funnel.ts", "now-minus-advanced", 1, "#1062 PR 4"),
    Offender("A", "apps/app-web/src/server/admin/ops.ts", "mart.current_pointer_head", 1, "#1062 PR 4"),
    Offender("B", "apps/app-web/src/server/admin/ops.ts", "mart.data_engine_identity", 1, "#1062 PR 4"),
)


def counted(violations: list[Violation]) -> Counter[tuple[str, str, str]]:
    return Counter(violation.key() for violation in violations)


def ratchet(violations: list[Violation], baseline: tuple[Offender, ...]) -> tuple[list[str], list[str]]:
    """(violations beyond the baseline, baseline entries that no longer match exactly)."""
    actual = counted(violations)
    allowed = {(entry.rule, entry.file, entry.pattern): entry.count for entry in baseline}
    new = [f"{violation}" for violation in violations if actual[violation.key()] > allowed.get(violation.key(), 0)]
    gone = [
        f"{entry.file} rule {entry.rule} {entry.pattern}: baseline says {entry.count}, "
        f"found {actual[(entry.rule, entry.file, entry.pattern)]}"
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
        if relation_references(literal.text, SERVED_HEAD)
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
        assert FOLLOW_UP.fullmatch(entry.follow_up), f"{entry} names no follow-up change"
        assert entry.count >= 1, entry
        assert (REPO_ROOT / entry.file).is_file(), f"{entry.file} is gone: delete its entries"
        assert entry.file.startswith(CONSUMER_ROOTS), f"{entry.file} is outside the consumer trees"
        if entry.rule == "A":
            assert entry.pattern in POINTER_RELATIONS, entry
        if entry.rule == "B":
            assert entry.pattern in SERVED, entry
        if entry.rule == "C":
            assert entry.pattern in {*SQL_AGE_PATTERNS, "stale-after-constant", "clock-with-advanced"}, entry


def test_the_health_endpoint_reads_the_served_head() -> None:
    """Fixed in #1062 itself, so it is not in the baseline."""
    relative = "apps/llm-service/src/llm_service/main.py"
    source = read_file(REPO_ROOT / relative)
    assert violations_of(relative, source) == []
    assert any(relation_references(literal.text, SERVED_HEAD) for literal in source.literals)
    assert not any(entry.file == relative for entry in BASELINE)


def test_no_consumer_reaches_a_relation_listed_as_not_served() -> None:
    """NOT_SERVED means that no consumer reads the relation. The list is checked, not trusted."""
    assert consumer_references(REPO_ROOT, tuple(NOT_SERVED)) == {}


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


# Shapes that a first version of the scan missed (audit of #1062). Each is one bypass of one rule.
# The expected violation names the relation in its lower-case, schema-qualified form.

A_HEAD = ("A", "mart.current_pointer_head")
B_GPPE = ("B", "mart.topt_gppe_results")
C_AGE = ("C", "clock-with-advanced")

TYPESCRIPT_BYPASSES = {
    "upper-case-name": ("export const SQL = `select run_id from MART.CURRENT_POINTER_HEAD`;\n", A_HEAD),
    "quoted-name": (r'export const SQL = "select run_id from \"mart\".\"current_pointer_head\"";' + "\n", A_HEAD),
    "spaced-dot": ("export const SQL = `select run_id from mart . current_pointer_head`;\n", A_HEAD),
    "unqualified-name": ("export const SQL = `select run_id from current_pointer_head`;\n", A_HEAD),
    "fragment-from-without-select": ("export const FROM = `from mart.current_pointer_head h`;\n", A_HEAD),
    "fragment-join-without-select": ("export const JOIN = ` join mart.current_pointer_head h on h.x = y.x`;\n", A_HEAD),
    "interpolated-schema": ("export const SQL = (S: string) => `select 1 from ${S}.current_pointer_head`;\n", A_HEAD),
    "regex-literal-with-a-backtick": (
        "export const TICK = /`/;\nexport const SQL = `select run_id from mart.current_pointer_head`;\n",
        A_HEAD,
    ),
    "regex-literal-with-a-quote-and-a-slash-class": (
        "export const RE = /['\"/]/g;\nexport const SQL = `select run_id from mart.current_pointer_head`;\n",
        A_HEAD,
    ),
    "upper-case-result-relation": ("export const SQL = `select 1 from MART.TOPT_GPPE_RESULTS`;\n", B_GPPE),
    "the-environments-view-is-not-the-served-head": (
        "export const SQL = `select 1 from mart.topt_gppe_results r join mart.served_head_environments e on true`;\n",
        B_GPPE,
    ),
}

PYTHON_BYPASSES = {
    "upper-case-name": ('SQL = "select run_id from MART.CURRENT_POINTER_HEAD"\n', A_HEAD),
    "quoted-name": ('SQL = \'select run_id from "mart"."current_pointer_head"\'\n', A_HEAD),
    "spaced-dot": ('SQL = "select run_id from mart . current_pointer_head"\n', A_HEAD),
    "unqualified-name": ('SQL = "select run_id from current_pointer_head"\n', A_HEAD),
    "fragment-from-without-select": ('FROM = "from mart.current_pointer_head h"\n', A_HEAD),
    "fragment-join-without-select": ('JOIN = " join mart.current_pointer_head h on h.x = y.x"\n', A_HEAD),
    "interpolated-schema": ('def sql(s):\n    return f"select 1 from {s}.current_pointer_head"\n', A_HEAD),
    "upper-case-result-relation": ('SQL = "select 1 from MART.TOPT_GPPE_RESULTS"\n', B_GPPE),
    "head-age-from-the-clock": (
        "from datetime import UTC, datetime\n\n\ndef age(head):\n    return datetime.now(UTC) - head.advanced_at\n",
        C_AGE,
    ),
    "head-age-from-a-clock-variable": (
        "def age(row, now):\n    return (now - row['advanced_at']).total_seconds() / 3600\n",
        C_AGE,
    ),
    "head-age-by-comparison": (
        "from datetime import UTC, datetime, timedelta\n\n\n"
        "def old(advanced_at):\n    return advanced_at < datetime.now(UTC) - timedelta(days=3)\n",
        C_AGE,
    ),
}


@pytest.mark.parametrize("shape", sorted(TYPESCRIPT_BYPASSES))
def test_a_typescript_bypass_shape_is_reported(tmp_path: Path, shape: str) -> None:
    source, (rule, pattern) = TYPESCRIPT_BYPASSES[shape]
    file = "apps/app-web/src/server/mart/shape.ts"
    _write(tmp_path, file, source)
    assert (rule, file, pattern) in _found(tmp_path), shape


@pytest.mark.parametrize("shape", sorted(PYTHON_BYPASSES))
def test_a_python_bypass_shape_is_reported(tmp_path: Path, shape: str) -> None:
    source, (rule, pattern) = PYTHON_BYPASSES[shape]
    file = "libs/contracts/src/truealpha_contracts/shape.py"
    _write(tmp_path, file, source)
    assert (rule, file, pattern) in _found(tmp_path), shape


def test_a_python_age_computation_that_does_not_touch_a_head_is_not_reported(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "libs/contracts/src/truealpha_contracts/elapsed.py",
        "from datetime import UTC, datetime\n\n\ndef elapsed(started_at):\n    return datetime.now(UTC) - started_at\n",
    )
    assert _found(tmp_path) == set()


def test_a_regex_literal_does_not_hide_a_compliant_statement_or_invent_a_violation(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "apps/app-web/src/server/mart/regex.ts",
        "export const TICK = /`/;\nconst half = total / 2 / count;\nconst tag = '</div>';\n"
        "export const SQL = `select run_id from mart.served_head`;\n",
    )
    assert _found(tmp_path) == set()


def test_a_prose_string_that_names_a_pointer_relation_is_not_a_reader(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "apps/app-web/src/server/admin/prose.ts",
        'export const NOTE = "mart.current_pointer_head - web, MCP and chat resolve the same governed run";\n'
        'export const CARD = "Traceable to a materialized output (mart.governed_strategy_run, schema: v1).";\n',
    )
    assert _found(tmp_path) == set()


def test_the_ratchet_reports_a_new_violation_and_a_stale_entry() -> None:
    reader = Violation("A", "apps/app-web/src/server/mart/a.ts", "mart.current_pointer_head", 7)
    entry = Offender("A", reader.file, reader.pattern, 1, "#1062 PR 3")
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


def test_a_consumer_file_that_names_a_not_served_relation_is_reported(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "apps/app-web/src/server/mart/backtests.ts",
        "export const SQL = `select run_id from mart.backtest_runs order by created_at desc limit 1`;\n",
    )
    _write(
        tmp_path,
        "libs/contracts/src/truealpha_contracts/meta.py",
        'SQL = "select 1 from MART.TOPT_CORE_META_INFO"\n',
    )
    _write(tmp_path, "apps/app-web/src/server/mart/quiet.ts", "// mart.backtest_runs in a comment only\n")
    assert consumer_references(tmp_path, tuple(NOT_SERVED)) == {
        "apps/app-web/src/server/mart/backtests.ts": ["mart.backtest_runs"],
        "libs/contracts/src/truealpha_contracts/meta.py": ["mart.topt_core_meta_info"],
    }


# --- completeness: every mart relation with a run key is served or explained ---------------


def test_the_served_and_not_served_lists_are_disjoint_and_explained() -> None:
    assert set(SERVED).isdisjoint(NOT_SERVED)
    assert len(set(SERVED)) == len(SERVED)
    assert all(len(reason) >= 20 for reason in NOT_SERVED.values()), "every NOT_SERVED entry needs a real reason"
    assert all(len(reason) >= 20 for reason in NOT_RUN_KEYS.values())


def _named(database: str) -> str:
    base = urlsplit(os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/truealpha"))
    return urlunsplit((base.scheme, base.netloc, f"/{database}", base.query, ""))


@pytest.fixture(scope="module")
def run_key_columns() -> Iterator[list[tuple[str, str]]]:
    """(relation, column) for every `mart` table or view column that looks like a run key.

    The filter is wide on purpose. A column that has `run` in its name, or ends in `snapshot_id`
    or `invocation_id`, is listed. The tests then decide which ones are real keys.
    """
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
                select 'mart.' || c.table_name, c.column_name
                from information_schema.columns c
                join information_schema.tables t using (table_schema, table_name)
                where c.table_schema = 'mart'
                  and t.table_type in ('BASE TABLE', 'VIEW')
                  and (c.column_name like '%run%'
                       or c.column_name like '%snapshot_id'
                       or c.column_name like '%invocation_id')
                """
            ).fetchall()
        yield [(row[0], row[1]) for row in rows]
    finally:
        with psycopg.connect(_named("postgres"), autocommit=True) as admin:
            admin.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name)))


def run_addressed_relations(columns: list[tuple[str, str]]) -> set[str]:
    """Relations with a run key, minus the pointer relations and the read point."""
    holders = {relation for relation, column in columns if column in RUN_KEY_COLUMNS}
    return holders - set(POINTER_RELATIONS) - {SERVED_HEAD}


def unknown_run_columns(columns: list[tuple[str, str]]) -> list[str]:
    known = set(RUN_KEY_COLUMNS) | set(NOT_RUN_KEYS)
    return sorted({f"{relation}.{column}" for relation, column in columns if column not in known})


def unclassified(relations: set[str], served: tuple[str, ...], not_served: dict[str, str]) -> list[str]:
    return sorted(relations - set(served) - set(not_served))


def test_every_run_like_column_is_a_run_key_or_explained(run_key_columns: list[tuple[str, str]]) -> None:
    assert len(run_key_columns) >= 25, f"the catalog read found only {len(run_key_columns)} columns"
    assert unknown_run_columns(run_key_columns) == [], (
        "a mart column looks like a run key and is in neither RUN_KEY_COLUMNS nor NOT_RUN_KEYS"
    )


def test_every_mart_relation_with_a_run_key_is_served_or_explained(run_key_columns: list[tuple[str, str]]) -> None:
    relations = run_addressed_relations(run_key_columns)
    assert len(relations) >= 20, f"the catalog read found only {sorted(relations)}"
    assert unclassified(relations, SERVED, NOT_SERVED) == [], (
        "a mart relation with a run key is in neither SERVED nor NOT_SERVED. A consumer that reads it "
        "serves a run. Put it in SERVED and read it through mart.served_head, or name why not in NOT_SERVED"
    )


def test_the_relations_that_escaped_the_first_version_are_now_classified(
    run_key_columns: list[tuple[str, str]],
) -> None:
    """`strategy_decisions` has only `strategy_run_id`. A check on `run_id` alone missed it."""
    relations = run_addressed_relations(run_key_columns)
    assert {"mart.strategy_decisions", "mart.strategy_runs", "mart.strategy_run_capture"} <= relations
    assert {"mart.strategy_decisions", "mart.strategy_runs", "mart.strategy_run_capture"} <= set(SERVED)


def test_no_listed_relation_is_missing_from_the_database(run_key_columns: list[tuple[str, str]]) -> None:
    """A list entry for a relation that is gone would outlive its defect."""
    relations = run_addressed_relations(run_key_columns)
    assert sorted((set(SERVED) | set(NOT_SERVED)) - relations) == []


def test_a_new_relation_or_a_new_key_name_is_reported() -> None:
    columns = [
        ("mart.brand_new_results", "run_id"),
        ("mart.other", "release_run_id"),
        ("mart.strategy_decisions", "strategy_run_id"),
    ]
    assert unclassified(run_addressed_relations(columns), SERVED, NOT_SERVED) == ["mart.brand_new_results"]
    assert unknown_run_columns(columns) == ["mart.other.release_run_id"]
    assert unclassified({"mart.strategy_decisions"}, ("mart.topt_gppe_results",), {}) == ["mart.strategy_decisions"]
