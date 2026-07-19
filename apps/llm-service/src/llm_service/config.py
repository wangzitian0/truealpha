from typing import Literal

from truealpha_runtime import RuntimeSettings


class Settings(RuntimeSettings):
    """Runtime settings layered on the shared runtime contract.

    `strategy_run_backend` defaults to `mart` -- the real
    `mart.strategy_runs`/`strategy_decisions` read (#362, retiring the
    checked-in golden fixture as the default consumer path). A real writer now
    populates the mart (the #414 replay writer and the #417 materialization
    asset), so the MCP `strategy_run` tool reads captured evidence, returning an
    honest `StrategyRunUnavailable(reason="no_runs_recorded")` only when the mart
    has genuinely not been materialized yet. `fixture` remains available for
    tests and offline previews, but is no longer the default.
    """

    strategy_run_backend: Literal["fixture", "mart"] = "mart"


settings = Settings()
