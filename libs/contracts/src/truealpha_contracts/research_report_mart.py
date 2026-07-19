"""Mart-backed `ResearchReadPort` — see #369, #429 (closed-issue drift audit).

`#369` was reopened by the drift audit: its closing PR shipped only the fixture-backed
`FixtureResearchReadRepository`, so `build_research_report` had no deployed consumer
reading real data. This module supplies that mart-backed reader.

`MartResearchReadRepository` subclasses `FixtureResearchReadRepository` and overrides
only the backing strategy-run repository (`PostgresStrategyRunRepository` instead of
`FixtureStrategyRunRepository`) and the provenance label. The ETF/missing-subject/
ranking-vs-company orchestration, and every section-builder helper, are inherited
unchanged — none of that logic is fixture-specific, so duplicating it here would just be
a second copy to keep in sync (`research_report_fixture.py`'s own docstring already
anticipated this: "swapping in a real mart-backed port later replaces this class only").
"""

from __future__ import annotations

from truealpha_contracts.research_report_fixture import FixtureResearchReadRepository
from truealpha_contracts.strategy_run import StrategyRunReadRepository
from truealpha_contracts.strategy_run_postgres import PostgresStrategyRunRepository


class MartResearchReadRepository(FixtureResearchReadRepository):
    """Reads already-materialized research sections from `mart.strategy_runs`/
    `strategy_decisions` via `PostgresStrategyRunRepository` (#362) — the same reader
    `/admin/strategy-runs` and, since #370, the App's `/research/*` dashboard use.

    `libs/contracts` classes take their database URL as an explicit argument rather than
    reading an environment variable themselves (`PostgresStrategyRunRepository` follows
    the same contract) — resolving which `DATABASE_URL` applies is the caller's job
    (a Dagster asset's own config, an MCP tool's `settings.database_url`, or a test's
    `os.environ`), not something this shared library should assume."""

    provenance_label = "mart:research_report.v1"

    def __init__(
        self,
        *,
        database_url: str | None = None,
        strategy_repository: StrategyRunReadRepository | None = None,
    ) -> None:
        if strategy_repository is not None:
            resolved = strategy_repository
        elif database_url is not None:
            resolved = PostgresStrategyRunRepository(database_url=database_url)
        else:
            raise TypeError("MartResearchReadRepository requires either database_url or strategy_repository")
        super().__init__(strategy_repository=resolved)
