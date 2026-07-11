from __future__ import annotations

import argparse

from truealpha_runtime.checks import run_dependency_checks
from truealpha_runtime.config import DeploymentSettings, RuntimeSettings
from truealpha_runtime.manifest import DEPENDENCY_MANIFEST


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate TrueAlpha runtime dependencies")
    parser.add_argument("check", nargs="?", choices=("check",), default="check")
    parser.add_argument("--live", action="store_true", help="probe PostgreSQL, KG tables, and object storage")
    args = parser.parse_args()
    settings = RuntimeSettings()
    DeploymentSettings()
    required = DEPENDENCY_MANIFEST.required_for(settings.environment_tier)
    print(f"runtime tier={settings.environment_tier.value} required={','.join(sorted(required))}")
    if not args.live:
        return 0
    results = run_dependency_checks(settings)
    for result in results:
        print(f"{result.name}: {result.status.value} ({result.duration_ms:.1f}ms) {result.detail}")
    return 0 if all(result.present for result in results if result.name in required) else 1
