#!/usr/bin/env python3
"""Environment manifests via infra2_sdk.manifests (#759)."""

from __future__ import annotations

import sys
from pathlib import Path

from infra2_sdk import manifests
from infra2_sdk.manifests import ManifestSpec, RuntimeHooksManifest
from infra2_sdk.runtime.config_schema import EnvironmentManifest

ROOT = Path(__file__).resolve().parents[4]
HOOKS_MANIFEST_PATH = "runtime-hooks.manifest.json"
SERVICE_ID = "truealpha/app"
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


def load_hooks(path: str = HOOKS_MANIFEST_PATH) -> RuntimeHooksManifest:
    return manifests.load_hooks_manifest(path, root=ROOT)


def check() -> list[str]:
    problems = manifests.check(root=ROOT, specs=SPECS, hand_written=HAND_WRITTEN)
    try:
        hooks = load_hooks()
    except (OSError, ValueError) as exc:
        problems.append(f"{HOOKS_MANIFEST_PATH}: {exc}")
    else:
        if hooks.service != SERVICE_ID:
            problems.append(f"{HOOKS_MANIFEST_PATH}: service is {hooks.service!r}, expected {SERVICE_ID!r}")
    return problems


def main(argv: list[str] | None = None) -> int:
    code = manifests.main(argv, root=ROOT, specs=SPECS, hand_written=HAND_WRITTEN, prog="env_manifest")
    args = sys.argv[1:] if argv is None else list(argv)
    if code == 0 and "--check" in args:
        try:
            hooks = load_hooks()
            if hooks.service != SERVICE_ID:
                raise ValueError(f"service is {hooks.service!r}, expected {SERVICE_ID!r}")
        except (OSError, ValueError) as exc:
            print(f"{HOOKS_MANIFEST_PATH}: {exc}")
            return 1
    return code


if __name__ == "__main__":
    sys.exit(main())
