#!/usr/bin/env python3
"""Release-derived deployment values for one environment (#759, infra2#622).

    uv run python tools/release_identity.py --env production

Prints the values infra2 injects into the data-engine compose environment at a release
SHA. Both are derived from this repository: RELEASE_MANIFEST_ID is a content hash,
CAPTURE_APPROVED_BY is the reviewed decision in governance/approvals/<env>.yaml.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml
from truealpha_contracts.release import live_release_manifest_id

ROOT = Path(__file__).resolve().parent.parent
ENVIRONMENTS = ("production", "staging")


def approval(env: str) -> dict[str, object]:
    if env not in ENVIRONMENTS:
        raise ValueError(f"unknown environment {env!r}")
    raw = yaml.safe_load((ROOT / "governance" / "approvals" / f"{env}.yaml").read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not str(raw.get("capture_approved_by", "")).strip():
        raise ValueError(f"governance/approvals/{env}.yaml must name capture_approved_by")
    return raw


def release_values(env: str) -> dict[str, str]:
    return {
        "RELEASE_MANIFEST_ID": live_release_manifest_id(),
        "CAPTURE_APPROVED_BY": str(approval(env)["capture_approved_by"]).strip(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env", required=True, choices=ENVIRONMENTS)
    args = parser.parse_args(argv)
    print(json.dumps(release_values(args.env), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
