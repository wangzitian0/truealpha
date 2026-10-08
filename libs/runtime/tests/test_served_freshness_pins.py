"""The served-freshness limits are stated once, and every copy is pinned to that one (#1062).

The authority is the seed in the migration. `init.md` states the same limits in a table for
readers, and `tools/datahub_freshness.py` bounds the pointer at the daily limit. A copy that
drifts from the seed fails here. No database is needed.

The workflow copy (`pointer_max_age_hours` in `deploy-freshness.yml`) is pinned to
`datahub_freshness.MAX_AGE_HOURS` by `test_ci_workflows.py`. This file pins `MAX_AGE_HOURS` to
the seed. Together the three copies cannot differ.
"""

from __future__ import annotations

import re
from pathlib import Path

from truealpha_runtime.testing import load_tool, read_seed_rows

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATION = REPO_ROOT / "db" / "migrations" / "20261006T1020_datahub_served_head_freshness.sql"
INIT = REPO_ROOT / "init.md"
QUALITY_REPORT = REPO_ROOT / "docs" / "datahub-quality-report.md"
HEADING = "### Served freshness (read time)"

_ROW = re.compile(r"^\|\s*`(?P<key>[a-z]+)`\s*\|\s*(?P<hours>\d+)\s*\|\s*(?P<days>\d+)\s*\|", re.MULTILINE)


def seed() -> dict[str, int]:
    return {str(row["limit_key"]): int(str(row["hours"])) for row in read_seed_rows(MIGRATION, "mart.freshness_limit")}


def init_text() -> str:
    return INIT.read_text(encoding="utf-8")


def section(text: str | None = None) -> str:
    """The text of the subsection: from its heading to the next heading or horizontal rule."""
    text = init_text() if text is None else text
    assert text.count(HEADING) == 1, f"init.md must hold exactly one {HEADING!r}"
    body = text.split(HEADING, 1)[1]
    return re.split(r"^(?:#{1,3} |---$)", body, maxsplit=1, flags=re.MULTILINE)[0]


def own_table(text: str | None = None) -> str:
    """The first table of the subsection. Rows of any other table are not limits."""
    lines = section(text).splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("|"))
    block = []
    for line in lines[start:]:
        if not line.startswith("|"):
            break
        block.append(line)
    return "\n".join(block)


def documented(text: str | None = None) -> dict[str, tuple[int, int]]:
    return {match["key"]: (int(match["hours"]), int(match["days"])) for match in _ROW.finditer(own_table(text))}


def test_the_init_table_equals_the_migration_seed() -> None:
    limits = seed()
    assert set(limits) == {"daily", "weekly", "quarterly", "withhold"}
    table = documented()
    assert {key: hours for key, (hours, _) in table.items()} == limits


def test_the_days_column_is_the_hours_column_over_24() -> None:
    assert documented(), "the reader found no table row"
    assert {key: days * 24 for key, (_, days) in documented().items()} == {
        key: hours for key, (hours, _) in documented().items()
    }


def test_no_limit_is_longer_than_thirty_days() -> None:
    assert max(seed().values()) == 720
    assert seed()["withhold"] == 720


def test_the_pointer_freshness_tool_bounds_at_the_daily_limit() -> None:
    tool = load_tool("datahub_freshness")
    assert tool.MAX_AGE_HOURS == seed()["daily"]


def test_the_section_names_the_reason_code_of_every_limit() -> None:
    text = section()
    for key, hours in seed().items():
        if key == "quarterly":
            continue  # equal to the cap: it never reads stale, so it has no stale reason
        assert f"older_than_{hours // 24}d" in text, (key, hours)


def test_the_quality_report_points_at_the_section() -> None:
    assert HEADING.removeprefix("### ").strip() in INIT.read_text(encoding="utf-8")
    assert '"Served freshness (read time)"' in QUALITY_REPORT.read_text(encoding="utf-8")


def test_the_section_holds_only_its_own_text() -> None:
    """The subsection once sat before an unrelated paragraph of section 8. That text then read as part of it."""
    body = section()
    assert body.lstrip().startswith("`mart.served_head` computes the age")
    assert "Environment and evidence scale" not in body
    assert "Release gates define claims" not in body
    assert "Known Risks" not in body
    text = init_text()
    assert text.index(HEADING) > text.index("Environment and evidence scale"), "the subsection ends section 8"
    assert text.index(HEADING) < text.index("## 9. Known Risks")


def test_the_reader_takes_the_first_table_only_and_stops_at_a_rule() -> None:
    synthetic = (
        f"intro\n\n{HEADING}\n\ntext\n\n| Family | Hours | Days |\n|---|---|---|\n"
        "| `daily` | 72 | 3 |\n\n| Other | Hours | Days |\n|---|---|---|\n| `other` | 5 | 1 |\n\n---\n\n"
        "| `after` | 9 | 9 |\n"
    )
    assert documented(synthetic) == {"daily": (72, 3)}
