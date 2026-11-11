"""Verify data-version replays and rollback trailers for a release (#1412 R4)."""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from typing import Any

DECLARATION = "apps/data-engine/release-replays.json"
_KEY = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_TAG_PATTERN = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")

Reader = Callable[[str, str], str | None]


class ReplayError(RuntimeError):
    """The release cannot prove its replays."""


def git_show(rev: str, path: str) -> str | None:
    result = subprocess.run(["git", "show", f"{rev}:{path}"], capture_output=True, text=True, check=False)
    return result.stdout if result.returncode == 0 else None


def newest_release_tag(target: str = "HEAD") -> tuple[str, str] | None:
    listed = subprocess.run(
        ["git", "tag", "--list", "v[0-9]*.[0-9]*.[0-9]*"], capture_output=True, text=True, check=False
    )
    candidates = [line.strip() for line in listed.stdout.splitlines() if _TAG_PATTERN.match(line.strip())]
    for name in sorted(candidates, key=lambda n: tuple(int(p) for p in n[1:].split(".")), reverse=True):
        peeled = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{name}^{{commit}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        sha = peeled.stdout.strip()
        if peeled.returncode == 0 and sha:
            ancestor = subprocess.run(
                ["git", "merge-base", "--is-ancestor", sha, target], capture_output=True, text=True, check=False
            )
            if ancestor.returncode == 0:
                return name, sha
    return None


def _without_docstrings(tree: ast.Module) -> ast.Module:
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    node.body = body[1:] or [ast.Pass()]
    return tree


def _assigned(tree: ast.Module, name: str) -> ast.expr | None:
    for node in reversed(tree.body):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return node.value
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            return node.value
    return None


def _parse(rev: str, path: str, text: str) -> ast.Module:
    try:
        return ast.parse(text)
    except SyntaxError as exc:
        raise ReplayError(f"{path} at {rev[:8]} does not parse: {exc}") from exc


def identity_value(rev: str, spec: dict[str, Any], read: Reader = git_show) -> str | None:
    if "name" in spec:
        text = read(rev, spec["path"])
        value = None if text is None else _assigned(_parse(rev, spec["path"], text), spec["name"])
        return None if value is None else ast.dump(value)
    parts = []
    for path in spec["paths"]:
        text = read(rev, path)
        if text is None:
            return None
        parts.append(ast.dump(_without_docstrings(_parse(rev, path, text))))
    return "\n".join(parts)


def _describe(key: str, spec: dict[str, Any]) -> str:
    return (
        f"{key} ({spec['path']} {spec['name']})"
        if "name" in spec
        else f"{key} (the code of {', '.join(spec['paths'])})"
    )


def _valid_identity_spec(key: str, spec: Any) -> bool:
    if not _KEY.match(key) or not isinstance(spec, dict):
        return False
    if isinstance(spec.get("path"), str) and isinstance(spec.get("name"), str):
        return True
    paths = spec.get("paths")
    return isinstance(paths, list) and bool(paths) and all(isinstance(p, str) for p in paths)


def _valid_replay_entry(entry: Any, identities: dict[str, Any]) -> bool:
    if not isinstance(entry, dict) or not isinstance(entry.get("identity"), str) or entry["identity"] not in identities:
        return False
    replay = entry.get("replay")
    return isinstance(replay, str) and bool(replay.strip()) and not _CONTROL.search(replay)


def parse_declaration(text: str | None, rev: str) -> dict[str, Any]:
    if text is None:
        raise ReplayError(f"{DECLARATION} is missing at {rev[:8]}. Restore the file.")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReplayError(f"{DECLARATION} at {rev[:8]} is not valid JSON: {exc}") from exc
    identities = data.get("identities") if isinstance(data, dict) else None
    replays = data.get("replays") if isinstance(data, dict) else None
    if not isinstance(identities, dict) or not identities or not isinstance(replays, list):
        raise ReplayError(f"{DECLARATION} at {rev[:8]} must hold a non-empty 'identities' object and a 'replays' list")
    for key, spec in identities.items():
        if not _valid_identity_spec(key, spec):
            raise ReplayError(
                f'{DECLARATION}: identity {key!r} must be a lowercase key with {{"path": ..., "name": ...}} or {{"paths": [...]}}'
            )
    for entry in replays:
        if not _valid_replay_entry(entry, identities):
            raise ReplayError(
                f"{DECLARATION}: replay entry {entry!r} must name a watched identity and a one-line, non-empty 'replay'"
            )
    return data


def trailers(base: str, base_tag: str, release: str, read: Reader = git_show) -> list[str]:
    declaration = parse_declaration(read(release, DECLARATION), release)
    try:
        earlier = parse_declaration(read(base, DECLARATION), base)["replays"]
    except ReplayError:
        earlier = []
    new = [entry for entry in declaration["replays"] if entry not in earlier]
    undeclared = []
    for key, spec in declaration["identities"].items():
        now = identity_value(release, spec, read)
        if now is None:
            raise ReplayError(
                f"{DECLARATION} watches {_describe(key, spec)}, which does not exist at {release[:8]}. "
                f"Point the identity at its new place in the same change that moves it."
            )
        if identity_value(base, spec, read) != now and not any(entry["identity"] == key for entry in new):
            undeclared.append(_describe(key, spec))
    if undeclared:
        raise ReplayError(
            f"the release changes data versions since {base_tag} with no declared replay: {'; '.join(undeclared)}. "
            f'Add one entry for each identity to the "replays" list of {DECLARATION}.'
        )
    lines = [f"Replay: {entry['identity']}: {entry['replay'].strip()}" for entry in new] or ["Replay: none"]
    return [*lines, f"Rollback-Tag: {base_tag}"]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--base-tag", required=True)
    parser.add_argument("--release", required=True)
    args = parser.parse_args(argv)
    try:
        lines = trailers(args.base, args.base_tag, args.release)
    except ReplayError as exc:
        print(f"release replays: {exc}", file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
