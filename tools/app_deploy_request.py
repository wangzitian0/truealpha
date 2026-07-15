"""Render a validated TrueAlpha staging DeployRequest without side effects."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlparse

from infra2_sdk.deploy import DeployOperation, DeployRequest, DeployType
from infra2_sdk.refs import classify_ref

SERVICE = "truealpha/app"
SOURCE_REPOSITORY = "wangzitian0/truealpha"
CONTRACT_VERSION = 1
_SOURCE_RUN_PATH_RE = re.compile(r"\A/wangzitian0/truealpha/actions/runs/([1-9][0-9]*)\Z")


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
) -> DeployRequest:
    """Build the only request shape this repository is currently allowed to emit."""
    return request_from_mapping(
        {
            "contract_version": CONTRACT_VERSION,
            "request_id": request_id,
            "operation": DeployOperation.DEPLOY.value,
            "service": SERVICE,
            "deploy_type": DeployType.STAGING.value,
            "version_ref": version_ref,
            "source_repository": SOURCE_REPOSITORY,
            "source_sha": source_sha,
            "evidence": {
                "source_run_url": source_run_url,
                "source_run_id": source_run_id,
                "staging_run_url": "",
                "reviewed_change_url": "",
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
    if deploy_type == DeployType.PRODUCTION.value:
        raise ValueError(
            "production requests are disabled until infra2 remotely verifies evidence and enables the receiver"
        )
    if deploy_type != DeployType.STAGING.value:
        raise ValueError("deploy_type must be staging")

    version_ref = raw.get("version_ref")
    if isinstance(version_ref, str):
        classify_ref(version_ref)

    evidence = raw.get("evidence")
    if not isinstance(evidence, Mapping):
        raise ValueError("evidence must be an object")
    source_run_url = evidence.get("source_run_url")
    if not isinstance(source_run_url, str) or not source_run_url:
        raise ValueError("evidence.source_run_url is required")
    parsed = urlparse(source_run_url)
    path_match = _SOURCE_RUN_PATH_RE.fullmatch(parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.params
        or parsed.query
        or parsed.fragment
        or path_match is None
    ):
        raise ValueError("evidence.source_run_url must point to the TrueAlpha GitHub Actions run")
    source_run_id = evidence.get("source_run_id")
    if not isinstance(source_run_id, str) or not source_run_id:
        raise ValueError("evidence.source_run_id is required")
    if source_run_id != path_match.group(1):
        raise ValueError("evidence.source_run_id must match source_run_url")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--version-ref", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--source-run-url", required=True)
    parser.add_argument("--source-run-id", required=True)
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
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(canonical_json(request), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
