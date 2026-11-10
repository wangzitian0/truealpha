# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import re; from pathlib import Path; REPO_ROOT = Path(__file__).resolve().parents[3]; MIGRATIONS = REPO_ROOT / "db/migrations"; _ADD = re.compile( r"add\s+constraint\s+(?P<name>[a-z_]+)\s+check\s*\((?P<body>.*?)\)\s*;", re.IGNORECASE | re.DOTALL, ); _IN_LIST = re.compile(r"\bin\s*\((?P<values>[^)]*)\)", re.IGNORECASE)
def _vocabularies() -> dict[str, list[tuple[str, set[str]]]]:
    found: dict[str, list[tuple[str, set[str]]]] = {}
    for path in sorted(MIGRATIONS.glob("*.sql")):
        source = path.read_text(encoding="utf-8")
        for match in _ADD.finditer(source):
            listed = _IN_LIST.search(match.group("body"))
            if listed is None: continue
            values = set(re.findall(r"'([^']+)'", listed.group("values")))
            if not values: continue
            found.setdefault(match.group("name"), []).append((path.name, values))
    return found
def test_no_migration_re_adds_a_narrower_vocabulary() -> None:
    offenders: list[str] = []
    for name, occurrences in _vocabularies().items():
        for index, (migration, values) in enumerate(occurrences):
            for later_migration, later_values in occurrences[index + 1 :]:
                missing = later_values - values
                if missing: offenders.append( f"{migration} re-adds {name} without {sorted(missing)}, which {later_migration} allows" )
    assert not offenders, ( "these migrations narrow a vocabulary a later one widens: " + "; ".join(offenders) + '. apply_migrations.sh replays the whole chain on every container boot, so the narrower list is validated against rows that need the wider one and the service never starts (#615)' )
def test_the_scan_sees_the_vocabularies_it_is_scanning() -> None: found = _vocabularies(); assert len(found) >= 3, ( f"only {len(found)} vocabularies parsed from {MIGRATIONS}; the scan is blind. Found: {sorted(found)}" )
