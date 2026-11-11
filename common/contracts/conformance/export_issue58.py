"""Export shared stable-identifier conformance metadata for Python/TypeScript parity."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from truealpha_contracts.common import STABLE_ID_PATTERN

OUTPUT_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = OUTPUT_DIR / "issue58.schemas.json"


def _render_json(payload: object) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def schema_bundle() -> dict[str, object]:
    return {
        "contract_set": "truealpha.issue-58.exact-contracts.v1",
        "stable_id_pattern": STABLE_ID_PATTERN,
    }


def write_artifacts() -> None:
    SCHEMA_PATH.write_text(_render_json(schema_bundle()), encoding="utf-8")


def check_artifacts() -> None:
    expected = _render_json(schema_bundle())
    if not SCHEMA_PATH.exists() or SCHEMA_PATH.read_text(encoding="utf-8") != expected:
        raise SystemExit(f"{SCHEMA_PATH} is out of date; run export_issue58.py --write")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="write conformance JSON artifacts")
    mode.add_argument("--check", action="store_true", help="verify committed conformance JSON artifacts")
    args = parser.parse_args()
    if args.write:
        write_artifacts()
    else:
        check_artifacts()


if __name__ == "__main__":
    main()
