"""What release this data engine is, measured from the artifact it runs as (#784, #712).

`truealpha_contracts.release` defines what the live release identity IS; this module reads
the three facts it is made of out of the running artifact:

    git_commit_sha                the release ref the deployment injected (`GIT_COMMIT_SHA`
                                  in infra2's compose -- the vX.Y.Z tag `pin_release`
                                  resolved), through `settings`, never `os.environ`.
    migration_ids                 the ordered ids of `db/migrations/*.sql` the image ships.
    environment_contract_sha256   infra2-sdk's `configuration_fingerprint` over the
                                  environment contract the image ships
                                  (`apps/data-engine/required-env.generated.json`).

Both files are read at their REPOSITORY paths relative to the process working directory,
which is `/app` in the deployed image (`WORKDIR /app`, and infra2's entrypoint execs the
command without changing it) and the repository root under `make`, `pytest` and
`tools/release_identity.py`. `apps/llm-service` already resolves its own manifest exactly
this way (`llm_service.main.ENV_MANIFEST_PATH`), and
`apps/data-engine/tests/test_release_identity.py` pins the Dockerfile `COPY` destinations
to the constants below so the two cannot drift apart.

Why files and not constants baked into this module: the value has to be identical in two
different filesystems -- the deploy runner's checkout at the release tag, and the container
built from it -- and the only way to prove that is to hash the same bytes in both. A
generated constant would only prove that a generator ran.

A missing file is a hard failure, never a default: an identity computed from what happened
to be present would be a *different* identity, silently, which is the failure this whole
change exists to remove.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from infra2_sdk import manifests
from infra2_sdk.runtime.config_schema import EnvironmentManifest, configuration_fingerprint
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.release import live_release_payload

from data_engine.config import settings

#: The environment contract this image ships and infra2's entrypoint boot-validates the
#: process environment against (`truealpha_runtime.boot.assert_environment`, #759).
ENV_MANIFEST_PATH = Path("apps/data-engine/required-env.generated.json")

#: The migration set this image ships. `db/apply_migrations.sh` applies `db/migrations/*.sql`
#: in shell glob order, so the ordered filenames ARE the applied schema (see
#: `libs/runtime/tests/test_migration_chain.py`).
MIGRATIONS_PATH = Path("db/migrations")


@dataclass(frozen=True)
class ReleaseIdentity:
    """One measurement of the running artifact's release identity."""

    payload: dict[str, object]
    content_sha256: str
    manifest_id: str


def migration_ids(path: Path | None = None) -> tuple[str, ...]:
    """The ordered ids of the migration set the artifact ships.

    Ordered by filename because that is the order `db/apply_migrations.sh` applies them in;
    the id is the stem, which is how `db/migrations/README.md` and the migration-chain guard
    name a migration.

    Ids rather than a hash of the files' bytes, because this repository's own frozen release
    contract already defines the migration set that way: `ReleaseManifest.migration_set_sha256`
    is `canonical_sha256(migration_ids)`. It also keeps the payload explainable when it is read
    back out of `staging.contract_objects` -- "which migrations was this release built on" is
    answerable, not just comparable. The trade is that an in-place edit of an ALREADY-RELEASED
    migration would not move the id; `db/migrations/README.md` forbids that ("the filename ...
    is permanent once an environment has run it", "never rename a migration that has reached
    staging or production") and `libs/runtime/tests/test_migration_chain.py` guards the chain.
    """
    directory = MIGRATIONS_PATH if path is None else Path(path)
    if not directory.is_dir():
        raise RuntimeError(
            f"the migration set {directory} is not in the image; the release identity cannot be measured (#784)"
        )
    ids = tuple(item.stem for item in sorted(directory.glob("*.sql"), key=lambda item: item.name))
    if not ids:
        raise RuntimeError(f"{directory} holds no migration; the release identity cannot be measured (#784)")
    return ids


def environment_contract(path: Path | None = None) -> EnvironmentManifest:
    """The environment contract the artifact ships, or a named failure."""
    target = ENV_MANIFEST_PATH if path is None else Path(path)
    if not target.is_file():
        raise RuntimeError(
            f"the environment manifest {target} is not in the image; the release identity cannot be "
            f"measured and infra2's entrypoint cannot boot-validate this environment (#759, #784)"
        )
    return manifests.load(target)


def environment_contract_sha256(path: Path | None = None) -> str:
    """Fingerprint the environment CONTRACT -- the declarations, not the values.

    `configuration_fingerprint` frames named inputs unambiguously and hashes each one before
    the outer digest. The inputs here are each field's full declaration, so the fingerprint
    moves when a variable is added, removed, renamed, or changes who produces it -- and does
    not move between staging and production, which is required: the deploy runner computes
    this id from a checkout at the release tag, with none of the deployed values in hand,
    and must reach the same answer as the container.
    """
    manifest = environment_contract(path)
    declarations = {
        field.env: json.dumps(field.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        for field in manifest.fields
    }
    return configuration_fingerprint(manifest, declarations)


def measure(
    *,
    git_commit_sha: str | None = None,
    migrations_path: Path | None = None,
    env_manifest_path: Path | None = None,
) -> ReleaseIdentity:
    """Measure this artifact's release identity.

    `git_commit_sha` defaults to what the deployment injected, read through `settings` so a
    process that resolved no value reports `unknown` rather than picking a stray environment
    variable up mid-run.
    """
    payload = live_release_payload(
        git_commit_sha=git_commit_sha if git_commit_sha is not None else (settings.git_commit_sha or "unknown"),
        migration_ids=migration_ids(migrations_path),
        environment_contract_sha256=environment_contract_sha256(env_manifest_path),
    )
    content_sha256 = canonical_sha256(payload)
    return ReleaseIdentity(
        payload=payload, content_sha256=content_sha256, manifest_id=f"release-manifest:{content_sha256}"
    )
