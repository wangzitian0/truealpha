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

Capacity refusals are the same kind of fact (rule 6, #729). The tally also listens to
the gateway (`gateway.on_capacity_refusal`) and counts every call a capacity gate refused
inside the tick, per ledger seat and kind — primary or corroborating, whichever adapter
made it — so an exhausted daily budget is a named line in the tick summary rather than a
run of cells that "had no data". A corroboration lost to such a refusal is recorded at
the `budget` stage (or `capacity` for any other refusal), not at `fetch`.
"""

from __future__ import annotations

import contextvars
import logging
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from data_engine.sources import gateway

log = logging.getLogger(__name__)

#: The origin raised or refused its answer; nothing reached the sink.
FETCH = "fetch"
#: The origin answered, but its bytes or rows could not be persisted.
PERSIST = "persist"
#: The rule-6 gate refused the origin's call: the seat's daily budget (this
#: environment's share of it) is spent. Nothing was sent.
BUDGET = "budget"
#: The rule-6 gate refused the origin's call for another reason (an undeclared seat, a
#: rate window that stayed full). Nothing was sent.
CAPACITY = "capacity"


def refusal_kind(error: gateway.CapacityExceeded) -> str:
    return BUDGET if isinstance(error, gateway.BudgetExhausted) else CAPACITY


@dataclass
class CorroborationTally:
    """Lost corroborations of one tick, per (origin, stage), and the calls its capacity
    gates refused, per (ledger seat, kind)."""

    lost: Counter[tuple[str, str]] = field(default_factory=Counter)
    refused: Counter[tuple[str, str]] = field(default_factory=Counter)

    @property
    def total(self) -> int:
        return sum(self.lost.values())

    @property
    def budget_exhausted(self) -> int:
        """Calls refused because a daily budget was spent — the figure the tick reports."""
        return sum(count for (_, kind), count in self.refused.items() if kind == BUDGET)

    @property
    def capacity_refused(self) -> int:
        return sum(self.refused.values())

    def summary(self) -> str:
        """`corroborations refused 0`, or the total with its breakdown:
        `corroborations refused 3 (moomoo-kline fetch 2, twelve-data budget 1)`."""
        return _counted("corroborations refused", self.lost)

    def refusals(self) -> str:
        """`capacity refused 0`, or the total with its breakdown per ledger seat and kind:
        `capacity refused 4 (twelvedata budget 3, yahoo capacity 1)`."""
        return _counted("capacity refused", self.refused)

    def note_refusal(self, error: gateway.CapacityExceeded) -> None:
        self.refused[(error.source, refusal_kind(error))] += 1


def _counted(label: str, counts: Counter[tuple[str, str]]) -> str:
    total = sum(counts.values())
    if not total:
        return f"{label} 0"
    parts = ", ".join(f"{name} {kind} {count}" for (name, kind), count in sorted(counts.items()))
    return f"{label} {total} ({parts})"


_tally: contextvars.ContextVar[CorroborationTally | None] = contextvars.ContextVar("corroboration_tally", default=None)


@contextmanager
def corroboration_tally() -> Iterator[CorroborationTally]:
    """Count every corroboration lost, and every call a capacity gate refused, inside
    the block."""
    tally = CorroborationTally()
    token = _tally.set(tally)
    try:
        with gateway.on_capacity_refusal(tally.note_refusal):
            yield tally
    finally:
        _tally.reset(token)


def record_lost_corroboration(origin: str, stage: str, subject: str, error: BaseException) -> None:
    """Log one lost corroboration and count it in the active tally, if any.

    A capacity refusal is recorded at its own stage whatever the caller passed: the
    origin did not fail, the gate declined to spend (#729). The refusal itself was
    already counted by the gate's listener.

    The traceback attached is `error`'s own: `exc_info=<exception instance>` is the
    stdlib form `Logger._log` expands to `(type, error, error.__traceback__)` (Python
    3.5+), so it never falls back to whatever `sys.exc_info()` holds at call time.
    """
    if isinstance(error, gateway.CapacityExceeded):
        stage = refusal_kind(error)
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
    "BUDGET",
    "CAPACITY",
    "FETCH",
    "PERSIST",
    "CorroborationTally",
    "corroboration_tally",
    "record_lost_corroboration",
    "refusal_kind",
)
