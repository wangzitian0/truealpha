"""One implementation of "what release is this environment serving".

#585. The fact that a deployed environment reports its release as a TAG
(``v0.0.20``), not a commit sha, was encoded independently in four places:

    tools/health_check.py         kind classification + version keys
    tools/deploy_freshness.py     read, validate, resolve
    tools/issue_close_guard.py    read, validate, resolve
    .github/workflows/deploy-freshness.yml   `curl | jq -r '.git_sha'`

The fourth is the one that runs daily and validated nothing: a non-object body
made ``jq`` emit ``null``, which reached ``walk_evidence.py`` as a release name
and produced "no 'Deploy prod null' run in the last 100 deploy-release runs" —
a true sentence about the wrong question. Two of the other three had learned to
reject a ref ``git`` would read as an option; the second learned it from review
after the first had already fixed it.

Where the four disagreed, this picks one answer deliberately rather than
inheriting whichever file was edited last:

    non-200                 error, naming the status
    missing / "unknown"     error — a check that cannot see the identity must
                            not go on to produce a verdict about it
    non-object JSON         error, naming the body prefix
    a ref git could misread  error, before the value is used for anything

All four are the same judgement: this module either returns an identity it is
willing to stand behind, or it raises. It never returns something a caller has
to re-validate, which is how the four copies came about.
"""

from __future__ import annotations

from infra2_sdk.deploy import (
    VERSION_KEYS,
    HttpGet,
    ReleaseIdentityError,
    identifier_kind,
    identity_from_body,
    read_deployed_release,
)

__all__ = [
    "HttpGet",
    "ReleaseIdentityError",
    "VERSION_KEYS",
    "identifier_kind",
    "identity_from_body",
    "read_deployed_release",
]
