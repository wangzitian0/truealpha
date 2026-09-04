"""Per-source registrations: the one place a source declares what it owns (#72).

`init.md` rule 22 asks that adding a source for an existing semantic type changes only
source-owned code, registrations, policies and tests — never generic capture,
manifest, snapshot or quality code. Until #72 the deployed composition root enumerated
the four semantics in five places (`SEMANTIC_TYPES`, the freshness dict, an
`if/elif` over semantic types in `build_routes`, `materialization._REQUIRED_TYPES`,
`quality_report._SOURCE_BY_PARSER`) and fabricated the registry ids it wrote on every
observation from the run label. This module replaces those enumerations with
derivations over `REGISTRATIONS`, and every source request now carries the content
hash of the registration that owns it.

A registration is data. The route builder it names is resolved by dotted path at
plan time (the `adapter_id` idea from `truealpha_contracts.registries`), so this module
imports no adapter and no adapter needs to import the composition root: the adapter
module owns its `build_route`, this module owns the list, the composition root owns
neither.

Ordering matters: obligations are expanded in `registered_semantic_types()` order and
their ordinals feed content-addressed identities, so the order below is the order the
deployed runs have always used.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from importlib import import_module
from typing import TYPE_CHECKING, Any, Protocol

from truealpha_contracts.common import canonical_sha256

from data_engine.datahub.production_topt.parser_identity import PARSER_VERSION_HISTORY

if TYPE_CHECKING:
    import psycopg

    from data_engine.datahub.production_topt.executor import SourceFetchPort


@dataclass(frozen=True)
class RouteCell:
    """One planned work item, as the owning source's route builder sees it."""

    work_item_id: str
    semantic_type: str
    issuer_id: str
    instrument_id: str
    listing_id: str
    ticker: str


@dataclass(frozen=True)
class RouteContext:
    """What every route builder may know about the run: the cutoffs, the resolved
    coordinates, and a connection for the source's own registries (or None in a
    plan-only build)."""

    cutoff: datetime
    cutoff_date: date
    price_cutoff_date: date
    partition_start: datetime
    coordinates: Mapping[str, tuple[str, str, str, str]]
    connection: psycopg.Connection[Any] | None


class RouteBuilder(Protocol):
    def __call__(self, context: RouteContext, cells: Sequence[RouteCell]) -> SourceFetchPort: ...


@dataclass(frozen=True)
class CapacityDeclaration:
    """The vendor's ceiling, declared next to the source (rule 6 as amended 2026-09-04,
    #729). Enforcement through the ledger is #729's; the declaration lives here so
    the gateway has something to enforce."""

    calls_per_window: int
    window_seconds: int
    daily_budget: int | None = None
    concurrency: int = 1


@dataclass(frozen=True)
class OriginRegistration:
    """One vendor origin behind a semantic: how its observations are recognised in the
    warehouse (parser vintages) and which payload key carries the value."""

    origin_source: str
    origin_id: str
    value_key: str
    parser_versions: tuple[str, ...]
    capacity: CapacityDeclaration | None = None


@dataclass(frozen=True)
class SourceRegistration:
    source_id: str
    version: str
    semantic_types: tuple[str, ...]
    freshness_max_age: Mapping[str, timedelta]
    #: "package.module:function" resolving to a `RouteBuilder`; resolved lazily so this
    #: module stays a leaf every adapter may import.
    route_builder: str
    origins: tuple[OriginRegistration, ...] = ()
    #: #579 corroboration class: A = second independent origin value-reconciled;
    #: B = single origin with domain falsifiers; release = release-frozen configuration.
    corroboration_class: str = "B"
    capacity: CapacityDeclaration | None = None
    ledger_seat: str | None = None
    #: The observation is a session close: it is knowable only once its session has
    #: settled, so a mid-session bar is not a close (#637) and a reused observation must
    #: be THE settled session's (#635). Declared by the source, applied by generic code.
    session_bound: bool = False
    notes: tuple[str, ...] = field(default=(), compare=False)

    def entry_payload(self) -> dict[str, Any]:
        """The declared facts, in a canonical shape, so the registration is content-addressed."""
        return {
            "source_id": self.source_id,
            "version": self.version,
            "semantic_types": list(self.semantic_types),
            "freshness_max_age_seconds": {k: int(v.total_seconds()) for k, v in sorted(self.freshness_max_age.items())},
            "route_builder": self.route_builder,
            "origins": [
                {
                    "origin_source": o.origin_source,
                    "origin_id": o.origin_id,
                    "value_key": o.value_key,
                    "parser_versions": list(o.parser_versions),
                }
                for o in self.origins
            ],
            "corroboration_class": self.corroboration_class,
            "session_bound": self.session_bound,
        }

    @property
    def entry_id(self) -> str:
        return f"source-registry-entry:{canonical_sha256(self.entry_payload())}"

    @property
    def policy_id(self) -> str:
        return f"source-policy:{self.source_id}:{self.version}"

    def resolve_route_builder(self) -> RouteBuilder:
        module_name, _, attribute = self.route_builder.partition(":")
        if not module_name or not attribute:
            raise ValueError(
                f"{self.source_id}: route_builder must be 'package.module:function', got {self.route_builder!r}"
            )
        return getattr(import_module(module_name), attribute)


# Twelve Data is the second origin behind market-price: its identity lives here (not
# in the adapter) so the quality report can recognise its vintages from the registry.
TWELVE_DATA_ORIGIN = "twelve-data"
TWELVE_DATA_PARSER_VERSION = "twelve-data-parser:v2"
TWELVE_DATA_MAPPING_VERSION = "twelve-data-map:v2"
TWELVE_DATA_VALUE_KEY = "close"

REGISTRATIONS: tuple[SourceRegistration, ...] = (
    SourceRegistration(
        source_id="yahoo-chart",
        version="v1",
        semantic_types=("market-price",),
        # A Friday bar is the freshest close at a Sunday or Monday-holiday 22:15 tick
        # (Fri 00:00 -> Tue 22:15 after a Monday holiday is ~4.9 days); 5 days admits
        # that and still fails a vendor serving week-old bars (#530 slice 2).
        freshness_max_age={"market-price": timedelta(days=5)},
        route_builder="data_engine.datahub.production_topt.market_price_adapter:build_route",
        origins=(
            OriginRegistration(
                origin_source="yahoo-chart:v1",
                origin_id="origin:yahoo:v1",
                value_key="close",
                # Every primary vintage ever written maps to Yahoo (#543: a bump
                # orphaned the warehouse's history once).
                parser_versions=PARSER_VERSION_HISTORY,
            ),
            OriginRegistration(
                origin_source="twelve-data:v1",
                origin_id="origin:twelve-data:v1",
                value_key=TWELVE_DATA_VALUE_KEY,
                parser_versions=(TWELVE_DATA_PARSER_VERSION,),
                # Free tier: 8 requests per minute, 800 per day, one key shared by
                # both environments (#491, #574).
                capacity=CapacityDeclaration(calls_per_window=8, window_seconds=60, daily_budget=800),
            ),
            OriginRegistration(
                origin_source="twelve-data:v1",
                origin_id="origin:twelve-data:v1",
                # The v1 parser wrote the value under `price` (#545's historical entry).
                value_key="price",
                parser_versions=("twelve-data-parser:v1",),
            ),
        ),
        corroboration_class="A",
        session_bound=True,
        notes=("Yahoo has no published quota; the second origin's capacity is the binding one.",),
    ),
    SourceRegistration(
        source_id="release-derived",
        version="v1",
        semantic_types=("listing-identity", "universe-membership"),
        # Release-frozen configuration; a year covers the universe's own refresh
        # cadence (#67), and the release manifest is the staleness authority.
        freshness_max_age={"listing-identity": timedelta(days=365), "universe-membership": timedelta(days=365)},
        route_builder="data_engine.datahub.production_topt.release_derived_adapter:build_route",
        corroboration_class="release",
    ),
    SourceRegistration(
        source_id="sec-company-facts",
        version="v1",
        semantic_types=("financial-fact",),
        # knowable_at is the filed date, months old by nature; 730 days aligns with the
        # factor's own period_end staleness bound (#534).
        freshness_max_age={"financial-fact": timedelta(days=730)},
        route_builder="data_engine.datahub.production_topt.sec_financial_adapter:build_route",
        corroboration_class="B",
        # SEC fair-access guidance: 10 requests per second per user agent.
        capacity=CapacityDeclaration(calls_per_window=10, window_seconds=1),
    ),
)


def registered_semantic_types() -> tuple[str, ...]:
    """Every semantic a registered source owns, in registration order (identity-bearing)."""
    return tuple(semantic for registration in REGISTRATIONS for semantic in registration.semantic_types)


def registration_for(semantic_type: str) -> SourceRegistration:
    for registration in REGISTRATIONS:
        if semantic_type in registration.semantic_types:
            return registration
    raise LookupError(f"no registered source owns the {semantic_type} semantic (#72)")


def semantic_types_of(source_id: str) -> frozenset[str]:
    for registration in REGISTRATIONS:
        if registration.source_id == source_id:
            return frozenset(registration.semantic_types)
    raise LookupError(f"no registration named {source_id}")


def freshness_windows() -> dict[str, timedelta]:
    return {
        semantic: window
        for registration in REGISTRATIONS
        for semantic, window in registration.freshness_max_age.items()
    }


def source_by_parser() -> dict[str, tuple[str, str, str]]:
    """parser_version -> (origin_source, origin_id, value_key), over every registered
    origin's history, so the quality report recognises every vintage ever written."""
    mapping: dict[str, tuple[str, str, str]] = {}
    for registration in REGISTRATIONS:
        for origin in registration.origins:
            for vintage in origin.parser_versions:
                mapping[vintage] = (origin.origin_source, origin.origin_id, origin.value_key)
    return mapping


SEMANTIC_TYPES: tuple[str, ...] = registered_semantic_types()
RELEASE_SEMANTICS: frozenset[str] = semantic_types_of("release-derived")
FRESHNESS_WINDOWS: dict[str, timedelta] = freshness_windows()
SOURCE_BY_PARSER: dict[str, tuple[str, str, str]] = source_by_parser()
