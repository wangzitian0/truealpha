# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import argparse; import json; import subprocess; import sys; from collections.abc import Sequence; from dataclasses import dataclass; from pathlib import Path; from truealpha_runtime.testing import DB_SKIP_GATE_MARKERS; REPO_ROOT = Path(__file__).resolve().parents[4]; MANIFEST = REPO_ROOT / "tools" / "mutations.json"; TIMEOUT_SECONDS = 300
@dataclass(frozen=True)
class Mutation: id: str; guard: str; file: str; find: str; replace: str; command: tuple[str, ...]; cwd: str
def load(path: Path = MANIFEST) -> list[Mutation]: raw = json.loads(path.read_text(encoding="utf-8")); return [ Mutation( id=entry["id"], guard=entry["guard"], file=entry["file"], find=entry["find"], replace=entry["replace"], command=tuple(entry["command"]), cwd=entry["cwd"], ) for entry in raw["mutations"] ]
_SKIP_GATE_MARKERS = DB_SKIP_GATE_MARKERS
def _skipped_under_db_gate(output: str) -> bool: return any(marker in output for marker in _SKIP_GATE_MARKERS)
def apply_and_run(mutation: Mutation) -> tuple[bool, str]:
    target = REPO_ROOT / mutation.file; original = target.read_text(encoding="utf-8"); matches = original.count(mutation.find)
    if matches == 0: return False, f"anchor not found in {mutation.file} — the mutation no longer applies"
    if matches > 1: return False, f"anchor matches {matches} times in {mutation.file} — pin it to a unique context"
    target.write_text(original.replace(mutation.find, mutation.replace, 1), encoding="utf-8")
    try: result = subprocess.run( list(mutation.command), cwd=REPO_ROOT / mutation.cwd, capture_output=True, text=True, check=False, timeout=TIMEOUT_SECONDS, )
    except subprocess.TimeoutExpired: return False, ( f'the guard did not finish within {TIMEOUT_SECONDS}s — it neither passed nor failed, so the property is unproven' )
    finally: target.write_text(original, encoding="utf-8")
    if result.returncode != 0: return True, ""
    combined_output = f"{result.stdout}\n{result.stderr}"
    if _skipped_under_db_gate(combined_output): return False, "the guard skipped under the DB write gate instead of proving the property"
    return False, "the guard passed with the property broken"
def run(only: str = "", mutations: Sequence[Mutation] | None = None) -> int:
    declared = list(mutations if mutations is not None else load())
    if only:
        declared = [mutation for mutation in declared if mutation.id == only]
        if not declared: print(f"no mutation named {only!r}", file=sys.stderr); return 1
    inert: list[str] = []
    for mutation in declared:
        caught, why = apply_and_run(mutation)
        if caught: print(f"  caught   {mutation.id}")
        else: print(f"  INERT    {mutation.id}: {why}"); inert.append(f"{mutation.id} ({mutation.guard}): {why}")
    if inert:
        print("", file=sys.stderr)
        for entry in inert: print(f"guard is inert: {entry}", file=sys.stderr)
        print( f'\n{len(inert)} of {len(declared)} declared properties can be broken without any guard noticing. Each was red once; each is now decoration.', file=sys.stderr, ); return 1
    print(f"\nall {len(declared)} declared properties are still guarded"); return 0
def _parser() -> argparse.ArgumentParser: parser = argparse.ArgumentParser(description=""); parser.add_argument("--only", default=""); return parser
def main(argv: Sequence[str] | None = None) -> int: return run(_parser().parse_args(argv).only)
if __name__ == "__main__": raise SystemExit(main())
