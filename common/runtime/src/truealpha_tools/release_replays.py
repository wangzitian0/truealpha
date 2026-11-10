# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import argparse; import ast; import json; import re; import subprocess; import sys; from collections.abc import Callable, Sequence; from typing import Any; DECLARATION = "apps/data-engine/release-replays.json"; _KEY = re.compile(r"^[a-z0-9][a-z0-9-]*$"); _CONTROL = re.compile(r"[\x00-\x1f\x7f]"); _TAG_PATTERN = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$"); Reader = Callable[[str, str], str | None]
class ReplayError(RuntimeError): """The release cannot prove its replays. The message names the fix."""
def git_show(rev: str, path: str) -> str | None: result = subprocess.run(["git", "show", f"{rev}:{path}"], capture_output=True, text=True, check=False); return result.stdout if result.returncode == 0 else None
def _tag_key(name: str) -> tuple[int, ...]: return tuple(int(part) for part in name[1:].split("."))
def newest_release_tag(target: str = "HEAD") -> tuple[str, str] | None:
    listed = subprocess.run( ["git", "tag", "--list", "v[0-9]*.[0-9]*.[0-9]*"], capture_output=True, text=True, check=False ); candidates = [line.strip() for line in listed.stdout.splitlines() if _TAG_PATTERN.match(line.strip())]
    for name in sorted(candidates, key=_tag_key, reverse=True):
        peeled = subprocess.run( ["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{name}^{{commit}}"], capture_output=True, text=True, check=False, ); sha = peeled.stdout.strip()
        if peeled.returncode != 0 or not sha: continue
        ancestor = subprocess.run( ["git", "merge-base", "--is-ancestor", sha, target], capture_output=True, text=True, check=False, )
        if ancestor.returncode == 0: return name, sha
    return None
def _without_docstrings(tree: ast.Module) -> ast.Module:
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str): node.body = body[1:] or [ast.Pass()]
    return tree
def _assigned(tree: ast.Module, name: str) -> ast.expr | None:
    for node in reversed(tree.body):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets): return node.value
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name: return node.value
    return None
def _parse(rev: str, path: str, text: str) -> ast.Module:
    try: return ast.parse(text)
    except SyntaxError as exc: raise ReplayError(f"{path} at {rev[:8]} does not parse: {exc}") from exc
def identity_value(rev: str, spec: dict[str, Any], read: Reader = git_show) -> str | None:
    if "name" in spec: text = read(rev, spec["path"]); value = None if text is None else _assigned(_parse(rev, spec["path"], text), spec["name"]); return None if value is None else ast.dump(value)
    parts = []
    for path in spec["paths"]:
        text = read(rev, path)
        if text is None: return None
        parts.append(ast.dump(_without_docstrings(_parse(rev, path, text))))
    return "\n".join(parts)
def _describe(key: str, spec: dict[str, Any]) -> str:
    if "name" in spec: return f"{key} ({spec['path']} {spec['name']})"
    return f"{key} (the code of {', '.join(spec['paths'])})"
def _load(text: str | None, rev: str) -> dict[str, Any]:
    if text is None: raise ReplayError( f'{DECLARATION} is missing at {rev[:8]}. Without it the release cannot show which data versions it changes. Restore the file.' )
    try: data = json.loads(text)
    except json.JSONDecodeError as exc: raise ReplayError(f"{DECLARATION} at {rev[:8]} is not valid JSON: {exc}") from exc
    identities = data.get("identities") if isinstance(data, dict) else None; replays = data.get("replays") if isinstance(data, dict) else None
    if not isinstance(identities, dict) or not identities or not isinstance(replays, list): raise ReplayError(f"{DECLARATION} at {rev[:8]} must hold a non-empty 'identities' object and a 'replays' list")
    return data
def _is_named_identity(spec: object) -> bool: return isinstance(spec, dict) and isinstance(spec.get("path"), str) and isinstance(spec.get("name"), str)
def _is_code_identity(spec: object) -> bool:
    if not isinstance(spec, dict) or not isinstance(spec.get("paths"), list) or not spec["paths"]: return False
    return all(isinstance(path, str) for path in spec["paths"])
def _check_identity(key: str, spec: object) -> None:
    if not _KEY.match(key) or not (_is_named_identity(spec) or _is_code_identity(spec)): raise ReplayError( f'{DECLARATION}: identity {key!r} must be a lowercase key with {{"path": ..., "name": ...}} or {{"paths": [...]}}' )
def _is_one_line_text(value: object) -> bool: return isinstance(value, str) and bool(value.strip()) and not _CONTROL.search(value)
def _check_replay(entry: object, identities: dict[str, Any]) -> None:
    valid = ( isinstance(entry, dict) and isinstance(entry.get("identity"), str) and entry["identity"] in identities and _is_one_line_text(entry.get("replay")) )
    if not valid: raise ReplayError( f"{DECLARATION}: replay entry {entry!r} must name a watched identity and a one-line, non-empty 'replay'" )
def parse_declaration(text: str | None, rev: str) -> dict[str, Any]:
    data = _load(text, rev)
    for key, spec in data["identities"].items(): _check_identity(key, spec)
    for entry in data["replays"]: _check_replay(entry, data["identities"])
    return data
def trailers(base: str, base_tag: str, release: str, read: Reader = git_show) -> list[str]:
    declaration = parse_declaration(read(release, DECLARATION), release)
    try: earlier = parse_declaration(read(base, DECLARATION), base)["replays"]
    except ReplayError: earlier = []
    new = [entry for entry in declaration["replays"] if entry not in earlier]; undeclared = []
    for key, spec in declaration["identities"].items():
        now = identity_value(release, spec, read)
        if now is None: raise ReplayError( f'{DECLARATION} watches {_describe(key, spec)}, which does not exist at {release[:8]}. Point the identity at its new place in the same change that moves it.' )
        if identity_value(base, spec, read) != now and not any(entry["identity"] == key for entry in new):
            undeclared.append(_describe(key, spec))
    if undeclared:
        raise ReplayError( f"""the release changes data versions since {base_tag} with no declared replay: {'; '.join(undeclared)}. Rows written under the old version stay until a replay rebuilds them (#1270). In a PR, add one entry for each identity to the "replays" list of {DECLARATION}, for example {{"identity": "<key>", "replay": "<the job and the config that rebuild the rows>"}}, then release again.""" )
    lines = [f"Replay: {entry['identity']}: {entry['replay'].strip()}" for entry in new] or ["Replay: none"]; return [*lines, f"Rollback-Tag: {base_tag}"]
def _parser() -> argparse.ArgumentParser: parser = argparse.ArgumentParser(description="", formatter_class=argparse.RawDescriptionHelpFormatter); parser.add_argument("--base", required=True, help="the commit of the base release tag"); parser.add_argument("--base-tag", required=True, help="the base release tag; it is also the rollback tag"); parser.add_argument("--release", required=True, help="the commit that the new tag will name"); return parser
def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try: lines = trailers(args.base, args.base_tag, args.release)
    except ReplayError as exc: print(f"release replays: {exc}", file=sys.stderr); return 1
    print("\n".join(lines)); return 0
if __name__ == "__main__": raise SystemExit(main())
