"""Ports implemented by runtime/storage and the future Postgres repositories."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Protocol, runtime_checkable

from truealpha_contracts.models import (
    AnalystRatingEvent,
    AsOfQuery,
    BacktestDataset,
    FinancialFact,
    FundHolding,
    GraphEdge,
    PriceBar,
    RawCapture,
    RawIngestionEnvelope,
    RawObjectRef,
)


@runtime_checkable
class RawObjectStore(Protocol):
    def store(self, capture: RawCapture) -> RawIngestionEnvelope: ...

    def get(self, ref: RawObjectRef) -> bytes: ...


@runtime_checkable
class PointInTimeRepository(Protocol):
    """Repository reads must resolve transaction time before factors see data."""

    def financial_facts(self, query: AsOfQuery) -> Sequence[FinancialFact]: ...

    def graph_edges(self, query: AsOfQuery) -> Sequence[GraphEdge]: ...

    def analyst_ratings(self, query: AsOfQuery) -> Sequence[AnalystRatingEvent]: ...

    def fund_holdings(self, query: AsOfQuery) -> Sequence[FundHolding]: ...

    def price_bars(self, query: AsOfQuery, *, start: date, end: date) -> Sequence[PriceBar]: ...


@runtime_checkable
class BacktestDataGateway(Protocol):
    """The only data boundary a backtest engine should consume."""

    def load(self, query: AsOfQuery, *, price_start: date, price_end: date) -> BacktestDataset: ...
