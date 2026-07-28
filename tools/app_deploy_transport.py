"""Dispatch one validated release request and prove the matching infra2 run succeeded."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import httpx
from infra2_sdk.dispatch import Api, LogFetcher, ReceiverRun, github_api_client
from infra2_sdk.dispatch import dispatch_and_wait as _sdk_dispatch_and_wait

from tools.app_deploy_request import request_from_mapping


def dispatch_and_wait(
    raw_request: Mapping[str, Any],
    *,
    api: Api,
    fetch_logs: LogFetcher,
    sleep: Callable[[float], None] = time.sleep,
    poll_interval: float = 5.0,
    max_attempts: int = 300,
) -> ReceiverRun:
    """Validate raw_request under TrueAlpha's sender policy, then delegate the
    watermark/ambiguity-guard/log-content-verification dispatch algorithm to
    infra2_sdk.dispatch -- the shared infra2-receiver-boundary implementation."""
    request = request_from_mapping(raw_request)
    return _sdk_dispatch_and_wait(
        request,
        api=api,
        fetch_logs=fetch_logs,
        sleep=sleep,
        poll_interval=poll_interval,
        max_attempts=max_attempts,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token-env", default="INFRA2_PAT")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--poll-interval", type=int, default=5)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    token = os.getenv(args.token_env, "")
    if not token:
        print(f"app deploy transport failed: {args.token_env} is required", file=sys.stderr)
        return 1
    if args.timeout <= 0 or args.poll_interval <= 0:
        print("app deploy transport failed: timeout and poll interval must be positive", file=sys.stderr)
        return 1
    try:
        raw = json.load(sys.stdin)
        if not isinstance(raw, Mapping):
            raise ValueError("deploy request must be a JSON object")
        api, fetch_logs = github_api_client(
            token=token,
            user_agent="truealpha-app-deploy-transport",
            timeout=30.0,
        )
        result = dispatch_and_wait(
            raw,
            api=api,
            fetch_logs=fetch_logs,
            poll_interval=float(args.poll_interval),
            max_attempts=max(1, (args.timeout + args.poll_interval - 1) // args.poll_interval),
        )
    except (ValueError, RuntimeError, httpx.HTTPError, json.JSONDecodeError) as exc:
        print(f"app deploy transport failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"receiver_run_id": result.run_id, "receiver_run_url": result.url}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
