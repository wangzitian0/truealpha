#!/usr/bin/env python3
"""Environment manifests: who produces every variable each app reads (#759).

A manifest is generated from a settings model (infra2-sdk contract v2) or, for the
TypeScript app, written by hand. infra2 derives Vault Agent templates, policies and the
daily reconciliation from these files; this repository only has to keep them true.

    uv run python tools/env_manifest.py --write         # regenerate
    uv run python tools/env_manifest.py --check         # CI: fresh + offline gate
    uv run python tools/env_manifest.py --validate-env apps/data-engine/required-env.generated.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from importlib import import_module
from pathlib import Path

from infra2_sdk.ci import validate_manifest_offline
from infra2_sdk.runtime.config_schema import (
    EnvironmentManifest,
    environment_manifest_from_model,
    validate_environment,
)

ROOT = Path(__file__).resolve().parent.parent
GENERATED: dict[str, tuple[str, str, str]] = {
    "apps/data-engine/required-env.generated.json": ("data_engine.config", "Settings", "apps/data-engine"),
    "apps/llm-service/required-env.generated.json": ("llm_service.config", "Settings", "apps/llm-service"),
}
HAND_WRITTEN: tuple[str, ...] = ("apps/app-web/required-env.manifest.json",)


def build(module: str, cls: str, source: str) -> EnvironmentManifest:
    return environment_manifest_from_model(getattr(import_module(module), cls), source=source)


def render(manifest: EnvironmentManifest) -> str:
    return json.dumps(manifest.to_dict(), indent=2) + "\n"


def load(path: str) -> EnvironmentManifest:
    return EnvironmentManifest.from_dict(json.loads((ROOT / path).read_text(encoding="utf-8")))


def all_manifests() -> dict[str, EnvironmentManifest]:
    manifests = {path: build(*spec) for path, spec in GENERATED.items()}
    manifests.update({path: load(path) for path in HAND_WRITTEN})
    return manifests


def check() -> list[str]:
    problems: list[str] = []
    for path, spec in GENERATED.items():
        expected = render(build(*spec))
        target = ROOT / path
        if not target.exists() or target.read_text(encoding="utf-8") != expected:
            problems.append(f"{path}: stale, run tools/env_manifest.py --write")
    for path, manifest in all_manifests().items():
        problems.extend(f"{path}: {error}" for error in validate_manifest_offline(manifest))
    return problems


def write() -> None:
    for path, spec in GENERATED.items():
        (ROOT / path).write_text(render(build(*spec)), encoding="utf-8")
        print(f"wrote {path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--validate-env", metavar="MANIFEST")
    args = parser.parse_args(argv)
    if args.write:
        write()
        return 0
    if args.check:
        problems = check()
        for problem in problems:
            print(problem)
        print("env manifests: ok" if not problems else f"env manifests: {len(problems)} problem(s)")
        return 1 if problems else 0
    result = validate_environment(load(args.validate_env), os.environ, require_injected=True)
    for name in result.missing:
        print(f"missing: {name}")
    print("environment: ok" if result.valid else f"environment: {len(result.missing)} missing")
    return 0 if result.valid else 1


if __name__ == "__main__":
    sys.exit(main())
