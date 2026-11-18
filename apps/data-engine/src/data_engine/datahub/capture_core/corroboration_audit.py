"""Corroboration loss and capacity refusal tallying."""

from __future__ import annotations

import contextvars
import logging
from collections import Counter
from collections.abc import Callable, Iterator, MutableMapping
from contextlib import contextmanager
from dataclasses import dataclass, field

from data_engine.sources import gateway

log = logging.getLogger(__name__)

FETCH = "fetch"
PERSIST = "persist"
BUDGET = "budget"
CAPACITY = "capacity"


def refusal_kind(error: gateway.CapacityExceeded) -> str:
    return BUDGET if isinstance(error, gateway.BudgetExhausted) else CAPACITY


@dataclass
class CorroborationTally:
    lost: Counter[tuple[str, str]] = field(default_factory=Counter)
    refused: Counter[tuple[str, str]] = field(default_factory=Counter)

    @property
    def total(self) -> int:
        return sum(self.lost.values())

    @property
    def budget_exhausted(self) -> int:
        return sum(count for (_, kind), count in self.refused.items() if kind == BUDGET)

    @property
    def capacity_refused(self) -> int:
        return sum(self.refused.values())

    def summary(self) -> str:
        return _counted("corroborations refused", self.lost)

    def refusals(self) -> str:
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
    tally = CorroborationTally()
    token = _tally.set(tally)
    try:
        with gateway.on_capacity_refusal(tally.note_refusal):
            yield tally
    finally:
        _tally.reset(token)


def record_lost_corroboration(origin: str, stage: str, subject: str, error: BaseException) -> None:
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


def cached_corroboration_fetch[K, V](
    cache: MutableMapping[K, V | None],
    key: K,
    origin: str,
    subject: str,
    fetch_fn: Callable[[], V | None],
    *,
    preload_fn: Callable[[], V | None] | None = None,
) -> V | None:
    if key in cache:
        return cache[key]
    if preload_fn is not None:
        stored = preload_fn()
        if stored is not None:
            cache[key] = stored
            return stored
    try:
        result = fetch_fn()
    except Exception as error:  # noqa: BLE001
        record_lost_corroboration(origin, FETCH, subject, error)
        result = None
    cache[key] = result
    return result


__all__ = (
    "BUDGET",
    "CAPACITY",
    "FETCH",
    "PERSIST",
    "CorroborationTally",
    "cached_corroboration_fetch",
    "corroboration_tally",
    "record_lost_corroboration",
    "refusal_kind",
)
