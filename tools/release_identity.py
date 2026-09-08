#!/usr/bin/env python3
"""Release-derived deployment values for one release of one environment (#784, #759, infra2#622).

    uv run python tools/release_identity.py --env production --version-ref v0.0.49

Prints the values infra2 injects into the data-engine compose environment for that release.
Both are derived from this repository at the release SHA:

* RELEASE_MANIFEST_ID is `data_engine.release_identity`'s measurement of the artifact --
  the release ref, the migration set under `db/migrations/`, and the fingerprint of
  `apps/data-engine/required-env.generated.json`. Run from a checkout at the release tag it
  reproduces exactly what the container built from that tag computes for itself, because
  both hash the same bytes.
* CAPTURE_APPROVED_BY is the reviewed decision in governance/approvals/<env>.yaml.

`--version-ref` is required and has no default: the id is a measurement OF a release, and a
value computed from an unnamed build is not the one a deployment should write down.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml
from data_engine.release_identity import measure

ROOT = Path(__file__).resolve().parent.parent
ENVIRONMENTS = ("production", "staging")


def approval(env: str) -> dict[str, object]:
    if env not in ENVIRONMENTS:
        raise ValueError(f"unknown environment {env!r}")
    raw = yaml.safe_load((ROOT / "governance" / "approvals" / f"{env}.yaml").read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not str(raw.get("capture_approved_by", "")).strip():
        raise ValueError(f"governance/approvals/{env}.yaml must name capture_approved_by")
    return raw


def release_values(env: str, *, version_ref: str) -> dict[str, str]:
    return {
        "RELEASE_MANIFEST_ID": measure(git_commit_sha=version_ref).manifest_id,
        "CAPTURE_APPROVED_BY": str(approval(env)["capture_approved_by"]).strip(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env", required=True, choices=ENVIRONMENTS)
    parser.add_argument(
        "--version-ref",
        required=True,
        help="the release this identity is OF: a vX.Y.Z tag or a commit sha (infra2's DEPLOY_VERSION_REF)",
    )
    args = parser.parse_args(argv)
    print(json.dumps(release_values(args.env, version_ref=args.version_ref), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
