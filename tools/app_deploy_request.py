"""Render a validated TrueAlpha release DeployRequest without side effects."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlparse

from infra2_sdk.deploy import DeployOperation, DeployRequest, DeployType

SERVICE = "truealpha/app"
SOURCE_REPOSITORY = "wangzitian0/truealpha"
CONTRACT_VERSION = 1
_SOURCE_RUN_PATH_RE = re.compile(r"\A/wangzitian0/truealpha/actions/runs/([1-9][0-9]*)\Z")
# infra2#571 blocker 2: this used to require an INFRA2 receiver-run URL, while
# infra2's verify_production_evidence requires the app's OWN staging run (a
# receiver run is never workflow_dispatch-triggered, so that check was
# unsatisfiable by construction) — the two sides of the pipeline contradicted each
# other and prod releases failed in both directions. infra2's expectation is the
# correct one: staging evidence is truealpha's own "Deploy staging" run (which
# itself polls the infra2 receiver to completion before succeeding), as declared in
# tools/production_evidence_policy.json (#464).
_STAGING_RUN_PATH_RE = re.compile(r"\A/wangzitian0/truealpha/actions/runs/([1-9][0-9]*)\Z")
_REVIEW_PATH_RE = re.compile(r"\A/wangzitian0/truealpha/pull/([1-9][0-9]*)\Z")
_SOURCE_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")
_SEMVER_TAG_RE = re.compile(r"\Av[0-9]+\.[0-9]+\.[0-9]+\Z")
_SUPPORTED_DEPLOY_TYPES = frozenset(
    {DeployType.PREVIEW_TAG.value, DeployType.STAGING.value, DeployType.PRODUCTION.value}
)


def request_from_mapping(raw: Mapping[str, Any]) -> DeployRequest:
    """Deserialize and enforce the narrower TrueAlpha sender policy."""
    _validate_authority(raw)
    request = DeployRequest.from_dict(raw)
    _validate_authority(request.to_dict())
    return request


def render_request(
    *,
    request_id: str,
    version_ref: str,
    source_sha: str,
    source_run_url: str,
    source_run_id: str,
    deploy_type: str = DeployType.STAGING.value,
    staging_run_url: str = "",
    reviewed_change_url: str = "",
) -> DeployRequest:
    """Build a release-only request for infra2's independently verified receiver."""
    return request_from_mapping(
        {
            "contract_version": CONTRACT_VERSION,
            "request_id": request_id,
            "operation": DeployOperation.DEPLOY.value,
            "service": SERVICE,
            "deploy_type": deploy_type,
            "version_ref": version_ref,
            "source_repository": SOURCE_REPOSITORY,
            "source_sha": source_sha,
            "evidence": {
                "source_run_url": source_run_url,
                "source_run_id": source_run_id,
                "staging_run_url": staging_run_url,
                "reviewed_change_url": reviewed_change_url,
            },
        }
    )


def canonical_json(request: DeployRequest) -> str:
    """Return stable wire bytes suitable for an explicitly separate sender."""
    return (
        json.dumps(
            request.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    )


def _validate_authority(raw: Mapping[str, Any]) -> None:
    contract_version = raw.get("contract_version")
    if isinstance(contract_version, bool) or contract_version != CONTRACT_VERSION:
        raise ValueError(f"contract_version must be {CONTRACT_VERSION}")
    if raw.get("service") != SERVICE:
        raise ValueError(f"service must be {SERVICE}")
    if raw.get("source_repository") != SOURCE_REPOSITORY:
        raise ValueError(f"source_repository must be {SOURCE_REPOSITORY}")
    if raw.get("operation") != DeployOperation.DEPLOY.value:
        raise ValueError("operation must be deploy")

    deploy_type = raw.get("deploy_type")
    if not isinstance(deploy_type, str) or deploy_type not in _SUPPORTED_DEPLOY_TYPES:
        raise ValueError("deploy_type must be preview/tag, staging, or prod")

    version_ref = raw.get("version_ref")
    if not isinstance(version_ref, str):
        raise ValueError("version_ref must be a string")
    if _SEMVER_TAG_RE.fullmatch(version_ref) is None:
        raise ValueError("version_ref must be a stable vX.Y.Z tag")

    source_sha = raw.get("source_sha")
    if not isinstance(source_sha, str) or _SOURCE_SHA_RE.fullmatch(source_sha) is None:
        raise ValueError("source_sha must be a lowercase 40-hex commit sha")

    evidence = raw.get("evidence")
    if not isinstance(evidence, Mapping):
        raise ValueError("evidence must be an object")
    source_run_url = evidence.get("source_run_url")
    if not isinstance(source_run_url, str) or not source_run_url:
        raise ValueError("evidence.source_run_url is required")
    path_match = _github_path_match(
        source_run_url,
        _SOURCE_RUN_PATH_RE,
        "evidence.source_run_url must point to the TrueAlpha GitHub Actions run",
    )
    source_run_id = evidence.get("source_run_id")
    if not isinstance(source_run_id, str) or not source_run_id:
        raise ValueError("evidence.source_run_id is required")
    if source_run_id != path_match.group(1):
        raise ValueError("evidence.source_run_id must match source_run_url")

    staging_run_url = evidence.get("staging_run_url")
    reviewed_change_url = evidence.get("reviewed_change_url")
    if staging_run_url is not None and not isinstance(staging_run_url, str):
        raise ValueError("evidence.staging_run_url must be a string")
    if reviewed_change_url is not None and not isinstance(reviewed_change_url, str):
        raise ValueError("evidence.reviewed_change_url must be a string")
    if deploy_type == DeployType.PRODUCTION.value:
        if not isinstance(staging_run_url, str) or not staging_run_url:
            raise ValueError("production evidence.staging_run_url is required")
        if not isinstance(reviewed_change_url, str) or not reviewed_change_url:
            raise ValueError("production evidence.reviewed_change_url is required")
        _github_path_match(
            staging_run_url,
            _STAGING_RUN_PATH_RE,
            "staging_run_url must point to this repo's own Deploy staging run",
        )
        _github_path_match(
            reviewed_change_url,
            _REVIEW_PATH_RE,
            "reviewed_change_url must point to a TrueAlpha pull request",
        )
    elif staging_run_url or reviewed_change_url:
        raise ValueError("non-production requests must not claim production evidence")


def _github_path_match(url: str, pattern: re.Pattern[str], message: str) -> re.Match[str]:
    parsed = urlparse(url)
    path_match = pattern.fullmatch(parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.params
        or parsed.query
        or parsed.fragment
        or path_match is None
    ):
        raise ValueError(message)
    return path_match


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--version-ref", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--source-run-url", required=True)
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument(
        "--deploy-type",
        choices=sorted(_SUPPORTED_DEPLOY_TYPES),
        default=DeployType.STAGING.value,
    )
    parser.add_argument("--staging-run-url", default="")
    parser.add_argument("--reviewed-change-url", default="")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        request = render_request(
            request_id=args.request_id,
            version_ref=args.version_ref,
            source_sha=args.source_sha,
            source_run_url=args.source_run_url,
            source_run_id=args.source_run_id,
            deploy_type=args.deploy_type,
            staging_run_url=args.staging_run_url,
            reviewed_change_url=args.reviewed_change_url,
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(canonical_json(request), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
