#!/usr/bin/env python3
"""Environment manifests: who produces every variable each app reads (#759).

A manifest is generated from a settings model (infra2-sdk contract v2) or, for the
TypeScript app, written by hand. infra2 derives Vault Agent templates, policies and the
daily reconciliation from these files; this repository only has to keep them true.

The driver -- rendering, the freshness check, the offline gate, boot-time validation -- is
`infra2_sdk.manifests` (SDK 1.5.0). This file names the specs and nothing else, so a
freshness rule or a gate fix lands in the SDK once instead of in every repository's copy.

    uv run python tools/env_manifest.py --write         # regenerate
    uv run python tools/env_manifest.py --check         # CI: fresh + offline gate
    uv run python tools/env_manifest.py --validate-env apps/data-engine/required-env.generated.json
"""

from __future__ import annotations

import sys
from pathlib import Path

from infra2_sdk import manifests
from infra2_sdk.manifests import ManifestSpec
from infra2_sdk.runtime.config_schema import EnvironmentManifest

ROOT = Path(__file__).resolve().parent.parent
SPECS: tuple[ManifestSpec, ...] = (
    ManifestSpec(
        "apps/data-engine/required-env.generated.json", "data_engine.config:Settings", source="apps/data-engine"
    ),
    ManifestSpec(
        "apps/llm-service/required-env.generated.json", "llm_service.config:Settings", source="apps/llm-service"
    ),
)
HAND_WRITTEN: tuple[str, ...] = ("apps/app-web/required-env.manifest.json",)


def load(path: str) -> EnvironmentManifest:
    return manifests.load(path, root=ROOT)


def all_manifests() -> dict[str, EnvironmentManifest]:
    return manifests.all_manifests(root=ROOT, specs=SPECS, hand_written=HAND_WRITTEN)


def check() -> list[str]:
    """Stale generated files plus offline-gate violations; empty when the manifests are true."""
    return manifests.check(root=ROOT, specs=SPECS, hand_written=HAND_WRITTEN)


def main(argv: list[str] | None = None) -> int:
    return manifests.main(argv, root=ROOT, specs=SPECS, hand_written=HAND_WRITTEN, prog="env_manifest")


if __name__ == "__main__":
    sys.exit(main())
