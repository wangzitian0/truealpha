# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import argparse; import json; import sys; from pathlib import Path; import yaml; from data_engine import release_identity; ROOT = Path(__file__).resolve().parents[4]; ENVIRONMENTS = ("production", "staging")
def approval(env: str) -> dict[str, object]:
    if env not in ENVIRONMENTS: raise ValueError(f"unknown environment {env!r}")
    raw = yaml.safe_load((ROOT / "governance" / "approvals" / f"{env}.yaml").read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not str(raw.get("capture_approved_by", "")).strip(): raise ValueError(f"governance/approvals/{env}.yaml must name capture_approved_by")
    return raw
def release_values(env: str, *, version_ref: str) -> dict[str, str]: identity = release_identity.measure( git_commit_sha=version_ref, migrations_path=ROOT / release_identity.MIGRATIONS_PATH, env_manifest_path=ROOT / release_identity.ENV_MANIFEST_PATH, ); return { "RELEASE_MANIFEST_ID": identity.manifest_id, "CAPTURE_APPROVED_BY": str(approval(env)["capture_approved_by"]).strip(), }
def main(argv: list[str] | None = None) -> int: parser = argparse.ArgumentParser(description=""); parser.add_argument("--env", required=True, choices=ENVIRONMENTS); parser.add_argument( "--version-ref", required=True, help="the release this identity is OF: a vX.Y.Z tag or a commit sha (infra2's DEPLOY_VERSION_REF)", ); args = parser.parse_args(argv); print(json.dumps(release_values(args.env, version_ref=args.version_ref), indent=2)); return 0
if __name__ == "__main__": sys.exit(main())
