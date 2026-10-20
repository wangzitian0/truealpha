"""Each release declares its data replays and its rollback tag (#1412 R4).

A changed parser, mapping or projector writes new rows only when it runs again. The rows that
the old code wrote stay. #1270: a projector fix reached main, and the next backtest ran on rows
that the old projector wrote. #1301: the first boot of an environment had no risk-free rates.
So a release names the replays that its change needs, before its tag claims a number.

`apps/data-engine/release-replays.json` is the one declaration. It holds two lists:

* `identities`: the version identities that this check watches.
  `{"path": P, "name": N}` is the value of the module-level assignment `N` in file `P`, for
  example a parser version tuple. `{"paths": [P, ...]}` is the code of whole modules, for a
  projector that has no version constant. Comments and docstrings are not part of an identity.
* `replays`: the declared replays, `{"identity": key, "replay": "what runs"}`. The PR that
  changes an identity adds an entry in the same change.

The check compares each identity at the base release commit with the release commit. A changed
identity needs at least one replay entry for that identity that the base declaration does not
hold, so an entry of an earlier release does not cover a later change. The check then prints the
tag trailers: one `Replay:` line for each new entry (or `Replay: none`) and `Rollback-Tag:`.

Stdlib only: `auto-release-staging.yml` runs `cut_release.sh --auto` with no `uv sync`.

Usage (the current directory is the repository):
  python3 tools/release_replays.py --base <commit> --base-tag vX.Y.Z --release <commit>

Exit 0: prints the trailer lines on stdout.
Exit 1: an identity changed with no new replay entry, or the declaration is missing or invalid.
"""

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
#: A trailer is one line. Control characters would split it or hide text in the tag annotation.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

Reader = Callable[[str, str], str | None]


class ReplayError(RuntimeError):
    """The release cannot prove its replays. The message names the fix."""


def git_show(rev: str, path: str) -> str | None:
    """The text of `path` at `rev`, or None when the commit does not hold the file."""
    result = subprocess.run(["git", "show", f"{rev}:{path}"], capture_output=True, text=True, check=False)
    return result.stdout if result.returncode == 0 else None


def _without_docstrings(tree: ast.Module) -> ast.Module:
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    node.body = body[1:] or [ast.Pass()]
    return tree


def _assigned(tree: ast.Module, name: str) -> ast.expr | None:
    """The value of the last module-level assignment to `name`."""
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
    """A comparable form of one identity at `rev`, or None when `rev` does not hold it.

    The form is an AST dump: it does not see comments, layout or line numbers. Both sides of
    one comparison are dumped by the same interpreter, so the form is never stored.
    """
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
    if "name" in spec:
        return f"{key} ({spec['path']} {spec['name']})"
    return f"{key} (the code of {', '.join(spec['paths'])})"


def _load(text: str | None, rev: str) -> dict[str, Any]:
    """The declaration as a JSON object with an 'identities' object and a 'replays' list."""
    if text is None:
        raise ReplayError(
            f"{DECLARATION} is missing at {rev[:8]}. Without it the release cannot show which data "
            f"versions it changes. Restore the file."
        )
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReplayError(f"{DECLARATION} at {rev[:8]} is not valid JSON: {exc}") from exc
    identities = data.get("identities") if isinstance(data, dict) else None
    replays = data.get("replays") if isinstance(data, dict) else None
    if not isinstance(identities, dict) or not identities or not isinstance(replays, list):
        raise ReplayError(f"{DECLARATION} at {rev[:8]} must hold a non-empty 'identities' object and a 'replays' list")
    return data


def _is_named_identity(spec: object) -> bool:
    """`{"path": P, "name": N}`: one module-level assignment."""
    return isinstance(spec, dict) and isinstance(spec.get("path"), str) and isinstance(spec.get("name"), str)


def _is_code_identity(spec: object) -> bool:
    """`{"paths": [P, ...]}`: the code of whole modules."""
    if not isinstance(spec, dict) or not isinstance(spec.get("paths"), list) or not spec["paths"]:
        return False
    return all(isinstance(path, str) for path in spec["paths"])


def _check_identity(key: str, spec: object) -> None:
    if not _KEY.match(key) or not (_is_named_identity(spec) or _is_code_identity(spec)):
        raise ReplayError(
            f'{DECLARATION}: identity {key!r} must be a lowercase key with {{"path": ..., "name": ...}} or {{"paths": [...]}}'
        )


def _is_one_line_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and not _CONTROL.search(value)


def _check_replay(entry: object, identities: dict[str, Any]) -> None:
    valid = (
        isinstance(entry, dict)
        and isinstance(entry.get("identity"), str)
        and entry["identity"] in identities
        and _is_one_line_text(entry.get("replay"))
    )
    if not valid:
        raise ReplayError(
            f"{DECLARATION}: replay entry {entry!r} must name a watched identity and a one-line, non-empty 'replay'"
        )


def parse_declaration(text: str | None, rev: str) -> dict[str, Any]:
    """The validated declaration at `rev`. Every defect is a refusal, never a silent pass."""
    data = _load(text, rev)
    for key, spec in data["identities"].items():
        _check_identity(key, spec)
    for entry in data["replays"]:
        _check_replay(entry, data["identities"])
    return data


def trailers(base: str, base_tag: str, release: str, read: Reader = git_show) -> list[str]:
    """The tag trailers of a release from `base` to `release`, or `ReplayError`."""
    declaration = parse_declaration(read(release, DECLARATION), release)
    try:
        earlier = parse_declaration(read(base, DECLARATION), base)["replays"]
    except ReplayError:
        # The base release predates the declaration, or its copy is broken. A release cannot
        # repair its base, so every entry of the release declaration counts as new.
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
        # An identity that the base does not hold is new, and a new identity counts as changed.
        if identity_value(base, spec, read) != now and not any(entry["identity"] == key for entry in new):
            undeclared.append(_describe(key, spec))
    if undeclared:
        raise ReplayError(
            f"the release changes data versions since {base_tag} with no declared replay: {'; '.join(undeclared)}. "
            f"Rows written under the old version stay until a replay rebuilds them (#1270). In a PR, add one entry "
            f'for each identity to the "replays" list of {DECLARATION}, for example '
            f'{{"identity": "<key>", "replay": "<the job and the config that rebuild the rows>"}}, then release again.'
        )
    lines = [f"Replay: {entry['identity']}: {entry['replay'].strip()}" for entry in new] or ["Replay: none"]
    return [*lines, f"Rollback-Tag: {base_tag}"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", required=True, help="the commit of the base release tag")
    parser.add_argument("--base-tag", required=True, help="the base release tag; it is also the rollback tag")
    parser.add_argument("--release", required=True, help="the commit that the new tag will name")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        lines = trailers(args.base, args.base_tag, args.release)
    except ReplayError as exc:
        print(f"release replays: {exc}", file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
