"""The migration chain must stay well-ordered — #576.

`db/apply_migrations.sh` re-applies EVERY file in `db/migrations/*.sql` on each
container boot, in shell glob order. That order is lexicographic over the whole
filename, so two migrations sharing a number are sequenced by the alphabet of
their suffixes rather than by intent: `0039_peg_module_1.sql` runs before
`0039_semantic_freshness_windows.sql` because "p" < "s".

Seven such collisions exist. #576 recorded one of them (a duplicate 0037) in
July and nothing was armed against it, so the v0.0.21 candidate added two more —
0039 and 0040, each a factors migration and a datahub migration written the same
week by two lanes that both took "the next number".

Nothing has broken yet, and that is luck rather than design: within each
colliding pair the two files touch disjoint objects. The day a pair touches the
same table, which one wins is decided by a filename.

The existing collisions are frozen below rather than renamed. Renaming changes
apply order in every environment that has already run them, which is a bigger
risk than the collision itself; freezing stops the bleeding without touching
what is deployed. The baseline may only shrink — an entry that is no longer a
collision fails, so it cannot outlive its defect.
"""

from __future__ import annotations

import collections
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = REPO_ROOT / "db/migrations"

#: Collisions that predate the guard. Frozen, never extended — a new number in
#: here means two lanes collided again and one of them must renumber before
#: merge, which is cheap while the migration is unreleased and expensive after.
KNOWN_COLLISIONS = {"0019", "0029", "0030", "0031", "0037", "0039", "0040"}


def _numbers() -> dict[str, list[str]]:
    by_number: dict[str, list[str]] = collections.defaultdict(list)
    for path in sorted(MIGRATIONS.glob("*.sql")):
        number = path.name.split("_", 1)[0]
        by_number[number].append(path.name)
    return by_number


def test_no_new_migration_number_collides() -> None:
    collisions = {number: names for number, names in _numbers().items() if len(names) > 1}
    fresh = set(collisions) - KNOWN_COLLISIONS
    assert not fresh, (
        f"migration number(s) {sorted(fresh)} are used twice: "
        + "; ".join(f"{number} -> {collisions[number]}" for number in sorted(fresh))
        + ". apply_migrations.sh replays the chain in glob order, so which of the two runs first "
        "is decided by the alphabet of the suffix, not by intent. Renumber the unreleased one "
        "(#576)"
    )


def test_the_frozen_collisions_are_still_collisions() -> None:
    """A baseline that outlives its defect hides the next one.

    Same rule the invariant exemptions carry: if a number was renumbered or a
    file deleted, the entry must leave this set rather than sit here granting
    permission nobody needs.
    """
    numbers = _numbers()
    stale = {number for number in KNOWN_COLLISIONS if len(numbers.get(number, [])) < 2}
    assert not stale, (
        f"{sorted(stale)} no longer collide — drop them from KNOWN_COLLISIONS so the set keeps "
        f"shrinking instead of granting permission for a collision that is gone"
    )


def test_every_migration_is_numbered() -> None:
    """The number is what orders the chain; a file without one sorts by its
    first letter and lands wherever that falls."""
    unnumbered = [path.name for path in sorted(MIGRATIONS.glob("*.sql")) if not re.match(r"\A\d{4}_", path.name)]
    assert not unnumbered, f"{unnumbered} carry no ordering number"
