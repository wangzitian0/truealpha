"""The migration chain must stay well-ordered — #576, #731.

`db/apply_migrations.sh` re-applies EVERY file in `db/migrations/*.sql` on each
container boot, in shell glob order. That order is lexicographic over the whole
filename, so two migrations sharing a number are sequenced by the alphabet of
their suffixes rather than by intent: `0039_peg_module_1.sql` runs before
`0039_semantic_freshness_windows.sql` because "p" < "s".

Seven such collisions exist. #576 recorded one of them (a duplicate 0037) in
July and nothing was armed against it, so the v0.0.21 candidate added two more —
0039 and 0040, each a factors migration and a datahub migration written the same
week by two lanes that both took "the next number". Four lanes added 47 migration
commits in the repository's first sixty days; "the next number" is a shared
counter with no lock.

Nothing has broken yet, and that is luck rather than design: within each
colliding pair the two files touch disjoint objects. The day a pair touches the
same table, which one wins is decided by a filename.

The existing collisions are frozen below rather than renamed. Renaming changes
apply order in every environment that has already run them, which is a bigger
risk than the collision itself; freezing stops the bleeding without touching
what is deployed. The baseline may only shrink — an entry that is no longer a
collision fails, so it cannot outlive its defect.

Two naming forms are therefore legal (#731):

* ``00NN_<slug>.sql`` — the legacy sequential form, FROZEN at ``0048``. No new
  file may take it: the shared counter is the defect.
* ``YYYYMMDDTHHMM_<lane>_<slug>.sql`` — the form every new migration uses. A
  minute-resolution UTC timestamp plus the lane name cannot collide across lanes
  working in parallel, and because ``"2"`` sorts after ``"0"`` every timestamped
  file applies after every legacy file: no environment that has already run the
  legacy chain reorders anything. See ``db/migrations/README.md``.
"""

from __future__ import annotations

import collections
import re
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = REPO_ROOT / "db/migrations"

#: Collisions that predate the guard. Frozen, never extended — a new number in
#: here means two lanes collided again and one of them must renumber before
#: merge, which is cheap while the migration is unreleased and expensive after.
KNOWN_COLLISIONS = {"0019", "0029", "0030", "0031", "0037", "0039", "0040"}

#: The last ordinal the sequential form ever takes. `0049_anything.sql` is red.
LEGACY_LAST_ORDINAL = "0048"

#: The workspace names that prefix issue and PR titles (AGENTS.md rule 3), so a
#: migration's filename says which lane owns it.
LANES = ("datahub", "factors", "bt", "web", "llm", "runtime")

LEGACY_RE = re.compile(r"\A(\d{4})_[a-z0-9_]+\.sql\Z")
TIMESTAMPED_RE = re.compile(r"\A(\d{8}T\d{4})_(" + "|".join(LANES) + r")_[a-z0-9_]+\.sql\Z")


def _names() -> list[str]:
    return sorted(path.name for path in MIGRATIONS.glob("*.sql"))


def _ordering_key(name: str) -> str:
    """What decides the file's slot: the ordinal (legacy) or timestamp+lane (new).

    Two files with the same key are applied in an order the filename's suffix
    decides, which is the defect this module exists to stop.
    """
    if match := LEGACY_RE.match(name):
        return match.group(1)
    if match := TIMESTAMPED_RE.match(name):
        return f"{match.group(1)}_{match.group(2)}"
    return name


def _by_key() -> dict[str, list[str]]:
    by_key: dict[str, list[str]] = collections.defaultdict(list)
    for name in _names():
        by_key[_ordering_key(name)].append(name)
    return by_key


def test_no_new_migration_number_collides() -> None:
    collisions = {key: names for key, names in _by_key().items() if len(names) > 1}
    fresh = set(collisions) - KNOWN_COLLISIONS
    assert not fresh, (
        f"migration ordering key(s) {sorted(fresh)} are used twice: "
        + "; ".join(f"{key} -> {collisions[key]}" for key in sorted(fresh))
        + ". apply_migrations.sh replays the chain in glob order, so which of the two runs first "
        "is decided by the alphabet of the suffix, not by intent. Rename the unreleased one "
        "(#576, #731)"
    )


def test_the_frozen_collisions_are_still_collisions() -> None:
    """A baseline that outlives its defect hides the next one.

    Same rule the invariant exemptions carry: if a number was renumbered or a
    file deleted, the entry must leave this set rather than sit here granting
    permission nobody needs.
    """
    by_key = _by_key()
    stale = {number for number in KNOWN_COLLISIONS if len(by_key.get(number, [])) < 2}
    assert not stale, (
        f"{sorted(stale)} no longer collide — drop them from KNOWN_COLLISIONS so the set keeps "
        f"shrinking instead of granting permission for a collision that is gone"
    )


def test_every_migration_takes_one_of_the_two_legal_forms() -> None:
    """A file outside both forms sorts by whatever its first character is and
    lands wherever that falls in the chain."""
    illegal = [name for name in _names() if not (LEGACY_RE.match(name) or TIMESTAMPED_RE.match(name))]
    assert not illegal, (
        f"{illegal} match neither `00NN_<slug>.sql` (legacy, frozen) nor "
        f"`YYYYMMDDTHHMM_<lane>_<slug>.sql` with lane in {LANES} (#731)"
    )


def test_the_sequential_form_is_frozen_at_the_last_legacy_ordinal() -> None:
    """The shared counter is the defect; new migrations name themselves by
    timestamp and lane so two parallel lanes cannot take the same slot."""
    too_new = [name for name in _names() if (match := LEGACY_RE.match(name)) and match.group(1) > LEGACY_LAST_ORDINAL]
    assert not too_new, (
        f"{too_new} take the retired sequential form; name new migrations "
        f"`{datetime.now().strftime('%Y%m%dT%H%M')}_<lane>_<slug>.sql` instead "
        f"(lanes: {', '.join(LANES)}; see db/migrations/README.md, #731)"
    )


def test_timestamped_migrations_carry_a_real_utc_minute() -> None:
    """The timestamp is the ordering key, so it must parse — `20261399T9999`
    would sort somewhere and mean nothing."""
    bad = []
    for name in _names():
        if match := TIMESTAMPED_RE.match(name):
            try:
                datetime.strptime(match.group(1), "%Y%m%dT%H%M")
            except ValueError:
                bad.append(name)
    assert not bad, f"{bad} carry a timestamp that is not a calendar minute"


def test_every_timestamped_migration_applies_after_the_whole_legacy_chain() -> None:
    """The property that makes the two forms safe to mix: glob order puts every
    `2026...` name after every `00NN_` name, so environments that already ran
    the legacy chain never see it reordered. Asserted over the real filenames,
    not assumed from the alphabet."""
    names = _names()
    legacy = [name for name in names if LEGACY_RE.match(name)]
    timestamped = [name for name in names if TIMESTAMPED_RE.match(name)]
    if legacy and timestamped:
        assert max(legacy) < min(timestamped), f"{min(timestamped)} would apply before {max(legacy)} in glob order"
    # And the order the runner sees is the order this module reasons about.
    assert names == sorted(names)
