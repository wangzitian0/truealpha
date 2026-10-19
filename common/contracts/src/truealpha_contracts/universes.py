"""Which universe a consumer serves.

`mart.current_pointer` is keyed `(environment, universe_id, universe_version, factor_id)`
and always was. Every consumer nonetheless resolved it with `where environment = ... and
factor_id = ... order by advanced_at desc limit 1`, throwing the universe away and serving
whichever pipeline advanced last. Once the canary universe started running the real
pipeline after each deploy, its 24-cell run displaced the 84-cell TOPT core and the
408-cell QQQ run in all five consumers -- which is how a module card came to read
"available" at 4% coverage.

The schema was never wrong. The consumers dropped the key, so the fix is a predicate, not a
migration.
"""

#: The universe the TOPT core surfaces serve. A prefix because the version moves with the
#: membership snapshot (`universe:topt-us-2026-03-31`) while the served identity does not.
SERVED_UNIVERSE_PREFIX = "universe:topt-"

__all__ = ["SERVED_UNIVERSE_PREFIX"]
