"""Live release identity contract (#784, #712).

Defines the content-addressed ``release-manifest:<sha256>`` identity over a running
artifact's git commit ref, ordered migration IDs, and environment contract digest.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

LIVE_RELEASE_KIND = "production-topt-live-release"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_REF = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._/-]{0,199}$")


def live_release_payload(
    *,
    git_commit_sha: str,
    migration_ids: Sequence[str],
    environment_contract_sha256: str,
) -> dict[str, object]:
    """Return the measured payload whose content hash is the live release identity."""
    ref = git_commit_sha.strip()
    if not _RELEASE_REF.fullmatch(ref):
        raise ValueError(f"git_commit_sha must be a usable release ref, got {git_commit_sha!r}")
    ids = tuple(str(value).strip() for value in migration_ids)
    if not ids or any(not value for value in ids):
        raise ValueError("migration_ids must be a non-empty sequence of non-empty ids")
    if len(set(ids)) != len(ids):
        raise ValueError("migration_ids must be unique")
    if not _SHA256.fullmatch(environment_contract_sha256):
        raise ValueError("environment_contract_sha256 must be a lowercase sha256 hex digest")
    return {
        "kind": LIVE_RELEASE_KIND,
        "git_commit_sha": ref,
        "migration_ids": list(ids),
        "environment_contract_sha256": environment_contract_sha256,
    }


__all__ = [
    "LIVE_RELEASE_KIND",
    "live_release_payload",
]
