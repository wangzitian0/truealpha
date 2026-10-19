"""Render a validated TrueAlpha release DeployRequest without side effects."""

from __future__ import annotations

import argparse
import re
from collections.abc import Mapping, Sequence
from typing import Any

from infra2_sdk.deploy import (
    DeployOperation,
    DeployRequest,
    DeployType,
    canonical_json,
    render_sender_request,
    validate_sender_request,
)

SERVICE = "truealpha/app"
SOURCE_REPOSITORY = "wangzitian0/truealpha"
CONTRACT_VERSION = 1
_REPO_LABEL = "TrueAlpha"
_STAGING_RUN_PATH_RE = re.compile(r"\A/wangzitian0/truealpha/actions/runs/[1-9][0-9]*\Z")
_SUPPORTED_DEPLOY_TYPES = (
    DeployType.PREVIEW_TAG.value,
    DeployType.STAGING.value,
    DeployType.PRODUCTION.value,
)

__all__ = [
    "CONTRACT_VERSION",
    "DeployOperation",
    "DeployRequest",
    "DeployType",
    "SERVICE",
    "SOURCE_REPOSITORY",
    "_STAGING_RUN_PATH_RE",
    "canonical_json",
    "main",
    "render_request",
    "request_from_mapping",
]


def request_from_mapping(raw: Mapping[str, Any]) -> DeployRequest:
    """Deserialize and enforce the narrower TrueAlpha sender policy."""
    return validate_sender_request(
        raw,
        service=SERVICE,
        source_repository=SOURCE_REPOSITORY,
        repo_label=_REPO_LABEL,
        supported_deploy_types=_SUPPORTED_DEPLOY_TYPES,
        version_ref_error="version_ref must be a stable vX.Y.Z tag",
        prod_staging_required_error="production evidence.staging_run_url is required",
        prod_review_required_error="production evidence.reviewed_change_url is required",
        prod_staging_url_error="staging_run_url must point to this repo's own Deploy staging run",
        prod_review_url_error="reviewed_change_url must point to a TrueAlpha pull request",
        non_prod_evidence_error="non-production requests must not claim production evidence",
    )


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
    return render_sender_request(
        service=SERVICE,
        source_repository=SOURCE_REPOSITORY,
        repo_label=_REPO_LABEL,
        request_id=request_id,
        version_ref=version_ref,
        source_sha=source_sha,
        source_run_url=source_run_url,
        source_run_id=source_run_id,
        deploy_type=deploy_type,
        staging_run_url=staging_run_url,
        reviewed_change_url=reviewed_change_url,
        supported_deploy_types=_SUPPORTED_DEPLOY_TYPES,
        version_ref_error="version_ref must be a stable vX.Y.Z tag",
        prod_staging_required_error="production evidence.staging_run_url is required",
        prod_review_required_error="production evidence.reviewed_change_url is required",
        prod_staging_url_error="staging_run_url must point to this repo's own Deploy staging run",
        prod_review_url_error="reviewed_change_url must point to a TrueAlpha pull request",
        non_prod_evidence_error="non-production requests must not claim production evidence",
    )


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
