"""Standing guard: no data-engine module writes a truthy restatement value (#530).

The vintage in mart lineage is the real mechanism. The read path resolves a
restatement with `knowable_at desc` (init.md Section 6). `NormalizedObservation`
keeps `is_restatement` and `supersedes_observation_id` declared. The capture
path never sets them.

This scan fails when a module under `apps/data-engine/src` starts to write a
value other than the default (`False` or `None`). Wire the two fields only after
an issue settles the comparison key. The comparison key needs the fact's own
period, which the bundled financial-fact observation does not carry yet.

The scan is static and uses the standard library only. It reads every source file
and imports none. The red-proof tests show that the scan fails on each write shape.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"

#: Each guarded field maps to the one literal that a source module may assign to it.
SAFE_LITERALS: dict[str, object] = {"is_restatement": False, "supersedes_observation_id": None}

#: The one reviewed SQL write site. It binds the validated model value as a parameter.
SQL_INSERT_ALLOWLIST = {"data_engine/datahub/repository.py"}

_INSERT_RE = re.compile(r"\binsert\s+into\b", re.IGNORECASE)
_UPDATE_RE = re.compile(r"\bupdate\b[^;]*\bset\b", re.IGNORECASE)
_TRUE_RE = re.compile(r"\btrue\b", re.IGNORECASE)


def _source_files(root: Path = SRC_ROOT) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _is_safe(field: str, node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is SAFE_LITERALS[field]


def _string_key(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _target_name(target: ast.expr) -> str | None:
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    if isinstance(target, ast.Subscript):
        return _string_key(target.slice)
    return None


def violations_in(source: str, rel: str, *, sql_insert_allowlist: set[str]) -> list[str]:
    """Every place where `source` writes a guarded field with a value other than the default."""
    found: list[str] = []
    tree = ast.parse(source, filename=rel)

    def flag(node: ast.AST, text: str) -> None:
        found.append(f"{rel}:{node.lineno} {text}")

    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg in SAFE_LITERALS:
            if not _is_safe(node.arg, node.value):
                flag(node.value, f"passes {node.arg}= a value other than {SAFE_LITERALS[node.arg]!r}")
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                name = _string_key(key)
                if name in SAFE_LITERALS and not _is_safe(name, value):
                    flag(value, f"maps {name!r} to a value other than {SAFE_LITERALS[name]!r}")
        elif isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if node.value is None:
                continue
            for target in targets:
                name = _target_name(target)
                if name in SAFE_LITERALS and not _is_safe(name, node.value):
                    flag(node, f"assigns {name} a value other than {SAFE_LITERALS[name]!r}")
        elif isinstance(node, ast.Call):
            callee = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            if callee in {"setattr", "__setattr__"} and any(_string_key(arg) in SAFE_LITERALS for arg in node.args):
                flag(node, "sets a guarded field by name")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            if not any(field in text for field in SAFE_LITERALS):
                continue
            if _UPDATE_RE.search(text):
                flag(node, "updates a guarded field in SQL")
            elif _INSERT_RE.search(text) and (rel not in sql_insert_allowlist or _TRUE_RE.search(text)):
                flag(node, "inserts a guarded field in SQL outside the reviewed site, or with a literal true")
    return found


def _scan_tree(*, sql_insert_allowlist: set[str]) -> list[str]:
    found: list[str] = []
    for path in _source_files():
        rel = path.relative_to(SRC_ROOT).as_posix()
        found.extend(violations_in(path.read_text(), rel, sql_insert_allowlist=sql_insert_allowlist))
    return found


def test_no_data_engine_module_writes_a_truthy_restatement_value() -> None:
    violations = _scan_tree(sql_insert_allowlist=SQL_INSERT_ALLOWLIST)
    assert not violations, "\n".join(violations)


def test_the_scan_reads_the_real_tree_and_sees_the_reviewed_read_site() -> None:
    # Guard against a scan that passes because it read nothing.
    rels = {path.relative_to(SRC_ROOT).as_posix() for path in _source_files()}
    assert "data_engine/datahub/repository.py" in rels
    repository = (SRC_ROOT / "data_engine/datahub/repository.py").read_text()
    reads = [
        node
        for node in ast.walk(ast.parse(repository))
        if isinstance(node, ast.Attribute) and node.attr in SAFE_LITERALS
    ]
    assert {node.attr for node in reads} == set(SAFE_LITERALS), "repository.py no longer passes both fields through"


def test_the_sql_rule_fires_on_the_real_insert_when_the_allowlist_is_empty() -> None:
    """Red-proof on real code: `repository.py` holds the one SQL insert that names both
    fields. Remove its allowlist entry and the same scan must flag it."""
    violations = _scan_tree(sql_insert_allowlist=set())
    assert any("data_engine/datahub/repository.py" in violation for violation in violations), (
        "removing the repository.py allowlist entry did not fail the scan: the SQL rule is vacuous"
    )


_WRITE_SHAPES = {
    "keyword true": "Obs(is_restatement=True)",
    "keyword computed": "Obs(is_restatement=prior is not None)",
    "keyword predecessor": "Obs(supersedes_observation_id=prior.observation_id)",
    "dict true": 'payload = {"is_restatement": True}',
    "dict predecessor": 'payload = {"supersedes_observation_id": prior_id}',
    "subscript true": 'payload["is_restatement"] = True',
    "attribute true": "obs.is_restatement = True",
    "attribute computed": "obs.supersedes_observation_id = prior_id",
    "augmented": "flags.is_restatement |= changed",
    "setattr": 'setattr(obs, "is_restatement", True)',
    "sql insert": 'conn.execute("insert into staging.capture_normalized_observations (is_restatement) values (%s)")',
    "sql literal true": 'conn.execute("insert into t (is_restatement, x) values (true, %s)")',
    "sql update": 'conn.execute("update t set is_restatement = %s where id = %s")',
}


@pytest.mark.parametrize("shape", sorted(_WRITE_SHAPES))
def test_the_scan_flags_each_write_shape(shape: str) -> None:
    violations = violations_in(_WRITE_SHAPES[shape], "rogue.py", sql_insert_allowlist=set())
    assert violations, f"the scan did not flag the {shape!r} shape"


_SAFE_SHAPES = {
    "default keyword": "Obs(is_restatement=False, supersedes_observation_id=None)",
    "class default": "class Model:\n    is_restatement: bool = False\n    supersedes_observation_id: str | None = None",
    "read": "value = obs.is_restatement or obs.supersedes_observation_id",
    "dict default": 'payload = {"is_restatement": False, "supersedes_observation_id": None}',
    "unrelated sql": 'conn.execute("insert into t (a) values (%s)")',
}


@pytest.mark.parametrize("shape", sorted(_SAFE_SHAPES))
def test_the_scan_accepts_the_default_and_read_only_shapes(shape: str) -> None:
    assert violations_in(_SAFE_SHAPES[shape], "safe.py", sql_insert_allowlist=set()) == []
