# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import argparse; import asyncio; from infra2_sdk.runtime.health import check_health; from truealpha_runtime.checks import DatabaseCheck, GraphStoreCheck, ObjectStorageCheck; from truealpha_runtime.config import RuntimeSettings; from truealpha_runtime.manifest import DEPENDENCY_MANIFEST
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate TrueAlpha runtime dependencies"); parser.add_argument("check", nargs="?", choices=("check", "health-check"), default="check"); parser.add_argument("--live", action="store_true", help="probe PostgreSQL, KG tables, and object storage"); args = parser.parse_args(argv); settings = RuntimeSettings(); required = DEPENDENCY_MANIFEST.required_for(settings.environment_tier); print(f"runtime tier={settings.environment_tier.value} required={','.join(sorted(required))}")
    if not args.live and args.check != "health-check": return 0
    checks = (DatabaseCheck(settings), GraphStoreCheck(settings), ObjectStorageCheck(settings)); status_code, body = asyncio.run( check_health( checks, manifest=DEPENDENCY_MANIFEST, tier=settings.environment_tier, ) )
    for check in checks: entry = body["checks"][check.name]; print(f"{check.name}: {entry['status']} ({entry['duration_ms']:.1f}ms) {entry['detail']}")
    return 0 if status_code == 200 else 1
