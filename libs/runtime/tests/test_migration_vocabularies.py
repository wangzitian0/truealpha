"""A migration may widen a value whitelist, never narrow one — #615.

`db/apply_migrations.sh` replays every migration on every container boot. A
migration that DROPs a check constraint and re-ADDs it from a literal list is
correct in order and fatal on replay: a later migration widens the list, real
rows use the wider vocabulary, and the earlier migration then validates the
narrower one against them. The chain aborts, the container exits, the service
crash-loops.

Twice now, on two tables, three weeks apart:

    contract_objects.contract_kind      0017/0018 narrow, 0038/0041 widen
                                        17 restart loops on production, 182 on staging
    pipeline_trigger_requests.job_name  0041 narrows to two values, 0045 widens
                                        to three. Staging's llm-service was at
                                        restarts=5; production carried two
                                        canary rows and would have failed its
                                        next boot.

#624 answered the first by seeding one row per contract kind and replaying. That
covers ONE table. Production carries seventeen value-whitelist constraints, and
seeding every allowed value of each is a lot of fixture for a property the
migration files already state.

So this reads the files: for one constraint name, no occurrence may omit a value
a LATER occurrence allows. No database and no rows.

WHAT IT DOES NOT COVER, stated because overstating it is how a guard becomes a
false comfort (review): only `in ('a', 'b')` enumerations are parsed.
`contract_objects_kind_identity_check` — the constraint behind the FIRST
incident — is a chain of `(kind = 'x' and id like 'x:%')` ORs and is invisible
here. That one is covered by the seeded replay #624 added to ci-db, and the two
mechanisms are complements, not one superset:

    in (...) enumerations        -> this file, statically, every such table
    OR/LIKE vocabularies         -> ci-db's third pass over seeded rows
    anything else                -> nothing yet

`test_the_scan_sees_the_vocabularies_it_is_scanning` pins the parse so a regex
that stops matching fails loudly instead of passing over an empty set.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = REPO_ROOT / "db/migrations"

#: `add constraint <name> ... check (... in ('a', 'b'))`, across newlines.
_ADD = re.compile(
    r"add\s+constraint\s+(?P<name>[a-z_]+)\s+check\s*\((?P<body>.*?)\)\s*;",
    re.IGNORECASE | re.DOTALL,
)
_IN_LIST = re.compile(r"\bin\s*\((?P<values>[^)]*)\)", re.IGNORECASE)


def _vocabularies() -> dict[str, list[tuple[str, set[str]]]]:
    """constraint name -> [(migration file, allowed values)], in chain order."""
    found: dict[str, list[tuple[str, set[str]]]] = {}
    for path in sorted(MIGRATIONS.glob("*.sql")):
        source = path.read_text(encoding="utf-8")
        for match in _ADD.finditer(source):
            listed = _IN_LIST.search(match.group("body"))
            if listed is None:
                continue
            values = set(re.findall(r"'([^']+)'", listed.group("values")))
            if not values:
                continue
            found.setdefault(match.group("name"), []).append((path.name, values))
    return found


def test_no_migration_re_adds_a_narrower_vocabulary() -> None:
    """Look FORWARD, not backward.

    The first version accumulated the widest list seen so far and compared each
    occurrence against it. That can never fire: the narrow one comes FIRST in
    chain order, so at the moment it is read nothing wider has been seen. It
    passed on the exact incident it was written from — 0041 narrowing what 0045
    widens — until the red case proved it inert.

    On a replay every migration runs again, so what matters for an occurrence is
    whether any LATER migration allows a value it would reject.
    """
    offenders: list[str] = []
    for name, occurrences in _vocabularies().items():
        for index, (migration, values) in enumerate(occurrences):
            for later_migration, later_values in occurrences[index + 1 :]:
                missing = later_values - values
                if missing:
                    offenders.append(
                        f"{migration} re-adds {name} without {sorted(missing)}, which {later_migration} allows"
                    )

    assert not offenders, (
        "these migrations narrow a vocabulary a later one widens: "
        + "; ".join(offenders)
        + ". apply_migrations.sh replays the whole chain on every container boot, so the "
        "narrower list is validated against rows that need the wider one and the service "
        "never starts (#615)"
    )


def test_the_scan_sees_the_vocabularies_it_is_scanning() -> None:
    """This file's whole value is the comparison; a parse that finds nothing
    passes silently and proves nothing. Production carries seventeen of these."""
    found = _vocabularies()
    assert len(found) >= 3, (
        f"only {len(found)} vocabularies parsed from {MIGRATIONS}; the scan is blind. Found: {sorted(found)}"
    )
