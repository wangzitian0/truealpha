"""Boot-time environment validation (#759): a process refuses to start on a missing name.

`required-env.generated.json` says which variables a service reads and who supplies each.
Until now that file bound only the deployer (templates, policies, the daily
reconciliation); the process itself started on whatever reached it and failed later,
wherever the value was first used. `assert_environment` closes that gap at the
entrypoint: the names the manifest declares and the environment lacks are the whole
error -- names, never values.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from infra2_sdk.manifests import validate_env


def assert_environment(
    manifest_path: str | Path, *, environ: Mapping[str, str] | None = None, require_injected: bool
) -> None:
    """Raise RuntimeError naming every variable `manifest_path` declares and `environ` lacks.

    `require_injected` is the deployed-tier rule: values the deployment injects (release
    identity, `APP_ENV`, the object-store endpoint) are required there and tolerated
    absent on a laptop or in CI, where nothing injects them. An empty value counts as
    absent. A manifest the image does not ship is its own red, named as such, because a
    missing file must never read as a clean bill.
    """
    path = Path(manifest_path)
    if not path.is_file():
        raise RuntimeError(f"environment manifest {path} is not in the image; boot validation cannot run (#759)")
    missing = validate_env(path, os.environ if environ is None else environ, require_injected=require_injected)
    if missing:
        raise RuntimeError(
            f"environment lacks {len(missing)} value(s) {path} declares: {', '.join(missing)} "
            f"-- names only; the deployment must inject them (#759)"
        )
