"""A lost corroboration is logged and counted, never silent (#885 items 1 and 3).

A second origin never fails the primary capture: an origin that raises, refuses its own
answer (`NotASessionCloseError`) or cannot be persisted leaves the cell single-origin
and the tick goes on. That promise used to be kept by bare `except Exception: continue`,
which made a revoked key, a dead OpenD or a whole tick of refused quantities look
exactly like "the vendor had nothing" until a pointer froze days later (#574/#622).

Every such site now calls `record_lost_corroboration`: a warning naming the origin, the
stage and the exception type (with the traceback), and one count in the tally of the
tick it happened in. The tally is scoped the way the external call ledger scopes its
run key (`gateway.run_scope`): a context variable the tick binds around the capture, so
the adapters, the vendor fetchers and the sink report into it without any signature
between the tick and them learning about it. Outside a tally (a script, a unit test)
the warning is still logged and nothing is counted.
"""

from __future__ import annotations

import contextvars
import logging
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: The origin raised or refused its answer; nothing reached the sink.
FETCH = "fetch"
#: The origin answered, but its bytes or rows could not be persisted.
PERSIST = "persist"


@dataclass
class CorroborationTally:
    """Lost corroborations of one tick, per (origin, stage)."""

    lost: Counter[tuple[str, str]] = field(default_factory=Counter)

    @property
    def total(self) -> int:
        return sum(self.lost.values())

    def summary(self) -> str:
        """`corroborations refused 0`, or the total with its breakdown:
        `corroborations refused 3 (moomoo-kline fetch 2, twelve-data persist 1)`."""
        line = f"corroborations refused {self.total}"
        if not self.total:
            return line
        parts = ", ".join(f"{origin} {stage} {count}" for (origin, stage), count in sorted(self.lost.items()))
        return f"{line} ({parts})"


_tally: contextvars.ContextVar[CorroborationTally | None] = contextvars.ContextVar("corroboration_tally", default=None)


@contextmanager
def corroboration_tally() -> Iterator[CorroborationTally]:
    """Count every corroboration lost inside the block."""
    tally = CorroborationTally()
    token = _tally.set(tally)
    try:
        yield tally
    finally:
        _tally.reset(token)


def record_lost_corroboration(origin: str, stage: str, subject: str, error: BaseException) -> None:
    """Log one lost corroboration and count it in the active tally, if any.

    Called from inside the `except` that absorbed `error`, so the traceback is attached.
    """
    log.warning(
        "corroborating origin %s lost at %s for %s: %s: %s — the cell stays single-origin",
        origin,
        stage,
        subject,
        type(error).__name__,
        error,
        exc_info=error,
    )
    tally = _tally.get()
    if tally is not None:
        tally.lost[(origin, stage)] += 1


__all__ = (
    "FETCH",
    "PERSIST",
    "CorroborationTally",
    "corroboration_tally",
    "record_lost_corroboration",
)
