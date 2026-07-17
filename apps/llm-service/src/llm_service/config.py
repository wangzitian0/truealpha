from typing import Literal

from truealpha_runtime import RuntimeSettings


class Settings(RuntimeSettings):
    """Runtime settings layered on the shared runtime contract.

    `strategy_run_backend` defaults to `fixture` -- the checked-in golden
    preview -- so deploying this setting is a no-op until it is explicitly
    flipped to `mart` once #26 lands a real writer into `mart.strategy_runs`
    (see #361). Flipping it before then just trades a rich preview report
    for an honest `StrategyRunUnavailable(reason="no_runs_recorded")`.
    """

    strategy_run_backend: Literal["fixture", "mart"] = "fixture"


settings = Settings()
