"""Dispatch one validated release request and prove the matching infra2 run succeeded."""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from infra2_sdk.deploy import Api, LogFetcher, ReceiverRun, run_deploy_transport_cli
from infra2_sdk.deploy import dispatch_and_wait as _sdk_dispatch_and_wait

from truealpha_tools.app_deploy_request import request_from_mapping


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
    infra2_sdk.deploy.dispatch_and_wait -- the shared infra2-receiver-boundary implementation."""
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
    return run_deploy_transport_cli(
        argv,
        dispatch=dispatch_and_wait,
        user_agent="truealpha-app-deploy-transport",
        stdin=sys.stdin,
    )


if __name__ == "__main__":
    raise SystemExit(main())
