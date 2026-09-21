"""The staging-only failover drill (#862): a primary price outage, rehearsed on purpose.

Failover is the path a healthy primary never takes, so until a vendor breaks nobody knows
whether it works on real data: after #897 shipped, production held zero `served_by_failover`
rows. A drill makes the break deliberately, for a few named tickers, and checks the result:

* **What breaks.** The primary price fetcher raises `SourceUnavailableError` for the drilled
  tickers — exactly what an unreachable Yahoo raises — so the executor spends its retries
  and asks `failover`. `PRIMARY_AND_TWELVE_DATA_UNAVAILABLE` also makes Twelve Data raise
  for those tickers, which proves the fall-through to moomoo. `PRIMARY_LAGGING` serves a lagged
  prior-session quote to test low-confidence failover. Every other ticker, and every
  other semantic, runs untouched.
* **Where it may run.** Never in production: `FailoverDrill.for_launch` refuses there, and
  `arm` refuses again at route build. A drill must also force a fetch (#874), so it reaches
  the vendors instead of reusing the night's observations, and it runs under a capture
  identity of its own (`drill_capture_version`).
* **How small.** At most `MAX_DRILL_TICKERS` tickers. Staging's Twelve Data share is
  3 credits a minute (`source_registrations.TWELVE_DATA_ENVIRONMENT_SHARES`); the drill
  itself asks no origin more than a forced tick already does, and a small set keeps the
  proof readable.
* **What it leaves behind.** The run plan and the quality report carry the drill
  (`plan_stamp`, `verdict`); the Dagster run carries the `truealpha/drill` tag; the governed
  pointer never advances to a drill run (`a1_evidence.unmet_objectives`); and the tick
  fails unless every drilled cell, and nothing else, was served by failover.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from truealpha_contracts.common import canonical_sha256

from data_engine.datahub.production_topt.source_registrations import TWELVE_DATA_ORIGIN

if TYPE_CHECKING:
    from data_engine.datahub.production_topt.market_price_adapter import (
        CorroboratingOrigin,
        MarketPriceFetcher,
        MarketPriceQuote,
    )

#: The Dagster run tag every drill run carries (value: the drill kind).
DRILL_TAG = "truealpha/drill"
#: The semantic a drill breaks: the only one with a registered failover.
DRILL_SEMANTIC = "market-price"
#: Staging's Twelve Data share is 3 credits/minute; three tickers keep a drill small.
MAX_DRILL_TICKERS = 3
#: The key the run plan and the quality report carry the drill under.
DRILL_KEY = "drill"
_PRODUCTION_NAMES = frozenset({"production", "prod"})


class DrillKind(StrEnum):
    PRIMARY_UNAVAILABLE = "primary_unavailable"
    PRIMARY_AND_TWELVE_DATA_UNAVAILABLE = "primary_and_twelve_data_unavailable"
    PRIMARY_LAGGING = "primary_lagging"


#: The failover origins each kind takes down besides the primary. A drilled cell served by
#: one of them would mean the drill did not break what it claims to.
_ORIGINS_DOWN: Mapping[DrillKind, tuple[str, ...]] = {
    DrillKind.PRIMARY_UNAVAILABLE: (),
    DrillKind.PRIMARY_AND_TWELVE_DATA_UNAVAILABLE: (TWELVE_DATA_ORIGIN,),
    DrillKind.PRIMARY_LAGGING: (),
}


class DrillRefused(ValueError):
    """A drill that must not run as launched."""


def is_production(app_env: str) -> bool:
    return app_env.strip().lower() in _PRODUCTION_NAMES


@dataclass(frozen=True)
class FailoverDrill:
    kind: DrillKind
    tickers: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.tickers:
            raise DrillRefused("a failover drill names at least one ticker")
        if len(self.tickers) > MAX_DRILL_TICKERS:
            raise DrillRefused(f"a failover drill names at most {MAX_DRILL_TICKERS} tickers, got {len(self.tickers)}")
        if tuple(sorted(set(self.tickers))) != self.tickers:
            raise DrillRefused("drill tickers must be distinct and sorted")

    @classmethod
    def for_launch(
        cls,
        *,
        app_env: str,
        force_fetch: bool,
        tickers: Sequence[str],
        twelve_data_unavailable: bool = False,
        primary_lagging: bool = False,
    ) -> FailoverDrill | None:
        """The drill a tick launch asks for, or None when it asks for none. Refused in
        production, without `force_fetch`, and beyond `MAX_DRILL_TICKERS`."""
        names = tuple(sorted({ticker.strip().upper() for ticker in tickers if ticker.strip()}))
        if not names:
            if twelve_data_unavailable:
                raise DrillRefused("drill_twelve_data_unavailable needs drill_primary_unavailable tickers")
            if primary_lagging:
                raise DrillRefused("drill_primary_lagging needs drill tickers")
            return None
        if is_production(app_env):
            raise DrillRefused(f"a failover drill never runs in production (APP_ENV={app_env!r})")
        if not force_fetch:
            raise DrillRefused("a failover drill must force a fetch (force_fetch: true), or it proves nothing")
        if primary_lagging and twelve_data_unavailable:
            raise DrillRefused("cannot combine primary_lagging with twelve_data_unavailable")
        if primary_lagging:
            kind = DrillKind.PRIMARY_LAGGING
        elif twelve_data_unavailable:
            kind = DrillKind.PRIMARY_AND_TWELVE_DATA_UNAVAILABLE
        else:
            kind = DrillKind.PRIMARY_UNAVAILABLE
        return cls(kind=kind, tickers=names)

    @property
    def origins_down(self) -> tuple[str, ...]:
        return _ORIGINS_DOWN[self.kind]

    @property
    def identity(self) -> str:
        return canonical_sha256({"kind": self.kind.value, "tickers": list(self.tickers)})

    def arm(
        self, app_env: str, fetcher: MarketPriceFetcher, origins: Sequence[CorroboratingOrigin]
    ) -> tuple[MarketPriceFetcher, tuple[CorroboratingOrigin, ...]]:
        """The primary fetcher and origins with the drilled tickers broken. Refused in
        production a second time: the route never trusts that its launch was checked."""
        if is_production(app_env):
            raise DrillRefused(f"a failover drill never arms in production (APP_ENV={app_env!r})")
        down = set(self.origins_down)
        if self.kind is DrillKind.PRIMARY_LAGGING:
            primary_fetcher = self._lagged(fetcher)
        else:
            primary_fetcher = self._broken(fetcher, "primary")
        return (
            primary_fetcher,
            tuple(
                replace(origin, fetch=self._broken(origin.fetch, origin.origin)) if origin.origin in down else origin
                for origin in origins
            ),
        )

    def _lagged(self, fetch: MarketPriceFetcher) -> Callable[[str, date], MarketPriceQuote | None]:
        from datetime import timedelta

        drilled = frozenset(self.tickers)

        def lagged_fetch(symbol: str, cutoff: date) -> MarketPriceQuote | None:
            if symbol in drilled:
                prior = cutoff - timedelta(days=1)
                while prior.weekday() >= 5:
                    prior -= timedelta(days=1)
                prior_quote = fetch(symbol, prior)
                if prior_quote is not None:
                    return prior_quote
                real_quote = fetch(symbol, cutoff)
                if real_quote is not None:
                    lag_days = (cutoff - prior).days
                    lagged_knowable = real_quote.knowable_at - timedelta(days=lag_days)
                    return replace(real_quote, as_of=prior, knowable_at=lagged_knowable)
                return None
            return fetch(symbol, cutoff)

        return lagged_fetch

    def _broken(self, fetch: MarketPriceFetcher, name: str) -> Callable[[str, date], MarketPriceQuote | None]:
        from data_engine.datahub.production_topt.market_price_adapter import SourceUnavailableError

        drilled = frozenset(self.tickers)

        def drilled_fetch(symbol: str, cutoff: date) -> MarketPriceQuote | None:
            if symbol in drilled:
                raise SourceUnavailableError(
                    f"failover drill ({self.kind.value}): {name} made unavailable for {symbol}"
                )
            return fetch(symbol, cutoff)

        return drilled_fetch

    def plan_stamp(self, coordinates: Mapping[str, tuple[str, str, str, str]], cells: int) -> dict[str, Any]:
        """What the run plan records: the drill, the listings it breaks and the cell count
        the run must serve by failover. `coordinates` is subject -> (issuer, instrument,
        listing, ticker); a drilled ticker outside the universe is refused, since a drill
        that breaks nothing proves nothing."""
        listings = {coordinate[3]: subject_id for subject_id, coordinate in coordinates.items()}
        missing = sorted(set(self.tickers) - set(listings))
        if missing:
            raise DrillRefused(f"drill tickers not in this run's universe: {', '.join(missing)}")
        return {
            "kind": self.kind.value,
            "tickers": list(self.tickers),
            "listing_ids": sorted(listings[ticker] for ticker in self.tickers),
            "origins_down": list(self.origins_down),
            "cells": cells,
        }


def drill_capture_version(version: str, drill: FailoverDrill) -> str:
    """A drill's capture version: its own identity, so it never lands on (or resumes) the
    forced or scheduled run of the same `executed_at`. Idempotent, like `forced_capture_version`."""
    suffix = f"-drill-{drill.identity[:12]}"
    return version if version.endswith(suffix) else f"{version}{suffix}"


def verdict(
    stamp: Mapping[str, Any], cells: Mapping[str, Mapping[str, Any]], served_by_failover: int
) -> dict[str, Any]:
    """The drill's result, read from the report's own reconciliation cells: it passes only
    when every drilled listing was served by failover, by an origin the drill did not take
    down, and the run's `served_by_failover_count` equals the drilled cell count."""
    listings = list(stamp.get("listing_ids") or ())
    down = set(stamp.get("origins_down") or ())
    served_by = {listing: cells.get(listing, {}).get("served_by_failover") for listing in listings}
    expected = int(stamp.get("cells") or 0)
    drilled_served = sum(1 for origin in served_by.values() if origin is not None and origin not in down)
    passed = expected > 0 and drilled_served == expected == served_by_failover
    return {
        **stamp,
        "served_by": served_by,
        "served_by_failover_count": served_by_failover,
        "passed": passed,
    }
