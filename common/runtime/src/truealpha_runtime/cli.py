from __future__ import annotations

import argparse
from typing import Protocol

from infra2_sdk.service import DependencyStatus, ProbeResult

from truealpha_runtime.checks import DatabaseCheck, GraphStoreCheck, ObjectStorageCheck
from truealpha_runtime.config import RuntimeSettings
from truealpha_runtime.manifest import DEPENDENCY_MANIFEST
from truealpha_runtime.storage import S3RawObjectStore, StorageError


class _ProbeCheck(Protocol):
    name: str

    def probe(self) -> ProbeResult: ...


def _run_probes(checks: tuple[_ProbeCheck, ...]) -> tuple[ProbeResult, ...]:
    return tuple(check.probe() for check in checks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate TrueAlpha runtime dependencies")
    parser.add_argument("check", nargs="?", choices=("check", "health-check", "ensure-bucket"), default="check")
    parser.add_argument("--live", action="store_true", help="probe PostgreSQL, KG tables, and object storage")
    args = parser.parse_args(argv)
    settings = RuntimeSettings()
    required = DEPENDENCY_MANIFEST.required_for(settings.environment_tier)
    print(f"runtime tier={settings.environment_tier.value} required={','.join(sorted(required))}")
    if args.check == "ensure-bucket":
        # Creates the bucket only in local and CI tiers (`may_create_bucket`); elsewhere it only checks access.
        try:
            S3RawObjectStore(settings).ensure_bucket()
        except (RuntimeError, StorageError) as exc:
            print(f"object_storage: {exc}")
            return 1
        print(f"object_storage: bucket {settings.s3_bucket} ready")
        return 0
    if not args.live and args.check != "health-check":
        return 0
    checks: tuple[_ProbeCheck, ...] = (DatabaseCheck(settings), GraphStoreCheck(settings), ObjectStorageCheck(settings))
    results = _run_probes(checks)
    unhealthy = False
    for check, result in zip(checks, results, strict=True):
        print(f"{check.name}: {result.status.value} ({result.duration_ms:.1f}ms) {result.detail}")
        if check.name in required and result.status is not DependencyStatus.PRESENT:
            unhealthy = True
    return 1 if unhealthy else 0
