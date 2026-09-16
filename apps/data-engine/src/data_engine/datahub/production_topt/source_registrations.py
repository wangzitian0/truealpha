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

Capacity (init.md rule 6, #729) is declared here too, once per ledger seat, in
`LEDGER_CAPACITIES`: a registration or origin names its seat and receives that
declaration, and `sources.gateway` derives the capacities it enforces from the same table.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from importlib import import_module
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

from truealpha_contracts.common import CaptureEnvironment, canonical_sha256

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
    # The governed universe head's publication time (#530 item 2): set only when the
    # run's universe was resolved from a governed head (`resolve_universe_corpus`);
    # None for the hand-curated TOPT corpus, which has no publication event of its own.
    universe_published_at: datetime | None
    coordinates: Mapping[str, tuple[str, str, str, str]]
    connection: psycopg.Connection[Any] | None


class RouteBuilder(Protocol):
    def __call__(self, context: RouteContext, cells: Sequence[RouteCell]) -> SourceFetchPort: ...


def environment_share(value: int, shares: Sequence[tuple[str, int]], environment: str) -> int | None:
    """``value`` scaled to ``environment``'s percentage of a shared allowance.

    Unsplit (no shares) is the whole value; an environment the split does not name gets
    None — it has no share, which the gateway refuses rather than reading as unlimited.
    Floored, so the environments' shares can never sum past the vendor's allowance.
    """
    if not shares:
        return value
    percent = dict(shares).get(environment)
    return None if percent is None else value * percent // 100


@dataclass(frozen=True)
class CapacityDeclaration:
    """What one ledger seat may spend (init.md §1 rule 6 as amended 2026-09-04, #729).

    Declared once, in `LEDGER_CAPACITIES` below, and nowhere else: `sources.gateway`
    derives its `CAPACITIES` from that table and enforces it before every gated call.
    (Until 2026-09-16 the gateway carried a hand-kept copy that disagreed with this one —
    SEC 8/s against 10/s, moomoo 60/30 s against 8/30 s — and nothing read this one.)

    - ``calls_per_window`` / ``window_seconds``: the pace the gateway queues calls to.
    - ``daily_budget``: calls per UTC day, checked against the environment's own
      ``staging.api_call_ledger`` before the call. None where neither the vendor nor we
      declare one: the seat is then paced, not budgeted.
    - ``concurrency``: declared, not enforced — every lane calls its vendors sequentially.
    - ``environment_shares``: ``(environment, percent)`` pairs for an allowance that ONE
      credential serves in several environments. Each environment gets its percentage of
      the rate window and of the daily budget and enforces it from its own ledger — the
      only ledger it can read (§3.1: environments never share one). The percentages sum to
      at most 100 and the scaled values are floored, so the environments can never jointly
      overspend the vendor. An environment the split does not name is refused.
    - ``documented``: the vendor's published limit the numbers are sized against, cited.
      Prose, so it is not part of the content-addressed payload.
    """

    calls_per_window: int
    window_seconds: int
    daily_budget: int | None = None
    concurrency: int = 1
    environment_shares: tuple[tuple[str, int], ...] = ()
    documented: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        for name in ("calls_per_window", "window_seconds", "concurrency"):
            if getattr(self, name) <= 0:
                raise ValueError(f"capacity {name} must be positive")
        if self.daily_budget is not None and self.daily_budget <= 0:
            raise ValueError("a daily budget must be positive; declare None for an unbudgeted seat")
        names = [environment for environment, _ in self.environment_shares]
        if len(names) != len(set(names)):
            raise ValueError(f"an environment appears twice in the split: {names}")
        known = {environment.value for environment in CaptureEnvironment}
        for environment, percent in self.environment_shares:
            if environment not in known:
                raise ValueError(f"{environment!r} is not a capture environment")
            if not 0 < percent <= 100:
                raise ValueError(f"{environment}'s share must be a percentage in (0, 100], got {percent}")
            for value in (self.calls_per_window, self.daily_budget):
                if value is not None and environment_share(value, self.environment_shares, environment) == 0:
                    raise ValueError(f"{environment}'s {percent}% of {value} floors to zero")
        if sum(percent for _, percent in self.environment_shares) > 100:
            raise ValueError(f"the environment shares sum past the vendor's allowance: {self.environment_shares}")


@dataclass(frozen=True)
class OriginRegistration:
    """One vendor origin behind a semantic: how its observations are recognised in the
    warehouse (parser vintages) and which payload key carries the value.

    ``ledger_seat`` names the `LEDGER_CAPACITIES` entry the origin's calls are charged
    to; ``capacity`` is filled from it (and refused when it says something else)."""

    origin_source: str
    origin_id: str
    value_key: str
    parser_versions: tuple[str, ...]
    capacity: CapacityDeclaration | None = None
    ledger_seat: str | None = None

    def __post_init__(self) -> None:
        _seat_capacity(self, self.origin_source)


def _seat_capacity(owner: OriginRegistration | SourceRegistration, name: str) -> None:
    """Fill ``owner.capacity`` from its ledger seat, the only place a number is written."""
    if owner.ledger_seat is None:
        return
    declared = LEDGER_CAPACITIES.get(owner.ledger_seat)
    if declared is None:
        raise ValueError(f"{name}: ledger seat {owner.ledger_seat!r} has no capacity declaration (rule 6)")
    if owner.capacity is not None and owner.capacity != declared:
        raise ValueError(
            f"{name}: capacity {owner.capacity} contradicts the {owner.ledger_seat!r} seat's declaration {declared}"
        )
    object.__setattr__(owner, "capacity", declared)


def _capacity_payload(capacity: CapacityDeclaration | None) -> dict[str, Any] | None:
    if capacity is None:
        return None
    return {
        "calls_per_window": capacity.calls_per_window,
        "window_seconds": capacity.window_seconds,
        "daily_budget": capacity.daily_budget,
        "concurrency": capacity.concurrency,
        "environment_shares": [list(pair) for pair in capacity.environment_shares],
    }


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

    def __post_init__(self) -> None:
        _seat_capacity(self, self.source_id)

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
                    "capacity": _capacity_payload(o.capacity),
                    "ledger_seat": o.ledger_seat,
                }
                for o in self.origins
            ],
            "corroboration_class": self.corroboration_class,
            "capacity": _capacity_payload(self.capacity),
            "ledger_seat": self.ledger_seat,
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
# Every second-origin vintage that writes its close under `close`, oldest first; the
# current one is the LAST entry, so a bump and the record of it are one edit (the same
# rule `PARSER_VERSION_HISTORY` follows, for the same #543 reason). v1 wrote `price` and
# is registered separately below.
TWELVE_DATA_PARSER_VERSION_HISTORY = ("twelve-data-parser:v2", "twelve-data-parser:v3")
TWELVE_DATA_PARSER_VERSION = TWELVE_DATA_PARSER_VERSION_HISTORY[-1]
TWELVE_DATA_MAPPING_VERSION = "twelve-data-map:v3"
TWELVE_DATA_VALUE_KEY = "close"

# moomoo OpenD is the THIRD origin behind market-price (a daily regular-session close
# from `request_history_kline`) and the SECOND origin behind financial-fact (annual
# income-statement and balance-sheet figures from `get_financials_statements`). Both
# identities live here, next to Twelve Data's, for the same reason: the quality report
# recognises a vintage from the registry, never from the adapter that wrote it.
MOOMOO_KLINE_ORIGIN = "moomoo-kline"
MOOMOO_KLINE_PARSER_VERSION = "moomoo-kline-parser:v1"
MOOMOO_KLINE_MAPPING_VERSION = "moomoo-kline-map:v1"
MOOMOO_KLINE_VALUE_KEY = "close"
MOOMOO_FINANCIALS_ORIGIN = "moomoo-financials"
MOOMOO_FINANCIALS_PARSER_VERSION = "moomoo-financials-parser:v1"
MOOMOO_FINANCIALS_MAPPING_VERSION = "moomoo-financials-map:v1"
# The headline key the registry reads back; the financial-fact fusion reads every
# corroborated field by name (`quality_report.FINANCIAL_FACT_FUSION_FIELDS`).
MOOMOO_FINANCIALS_VALUE_KEY = "revenue"
# -- rule 6: every ledger seat's capacity, declared once (#729) ---------------------------
#
# The keys are the `source` a call is recorded under in `staging.api_call_ledger`. Numbers
# are sized against the vendor's published limit (cited per entry) and against the
# ledger's measured traffic: production's busiest day of 2026-09-08..15 was 152 yahoo /
# 152 twelvedata / 354 sec calls, staging's 42 twelvedata / 1,824 sec (the standards
# backfill of 2026-09-15). A ceiling the vendor does not publish is ours and says so.

#: One Twelve Data key serves staging and production (#491, #574), so its allowance is
#: split: production runs three universes (TOPT, QQQ, canary), staging TOPT and the
#: canary, with QQQ only on a manual trigger. 60 % is 4 credits/minute and 480/day
#: (production spent at most 152); 40 % is 3/minute and 320/day (staging at most 42,
#: ~250 with a manual QQQ). The two minute shares sum to 7 of 8, so the environments'
#: concurrent canaries (both at 23:47 UTC — the 2026-09-07 23:48 ledger rows show 18
#: credits in one minute across the two) can no longer jointly trip the vendor's window.
TWELVE_DATA_ENVIRONMENT_SHARES: tuple[tuple[str, int], ...] = (("production", 60), ("staging", 40))

LEDGER_CAPACITIES: Mapping[str, CapacityDeclaration] = MappingProxyType(
    {
        # SEC fair access allows at most 10 requests per second per requester and
        # publishes no daily cap. 8/s is declared: both environments reach SEC from one
        # host, and before this gate a production tick fired 18 `submissions` requests
        # in one second (2026-09-08 22:15:35). 5,000/day is our runaway backstop, per
        # environment (2.7x staging's busiest day).
        "sec": CapacityDeclaration(
            calls_per_window=8,
            window_seconds=1,
            daily_budget=5000,
            documented="SEC EDGAR fair access: max 10 requests/second; no daily cap",
        ),
        # N-PORT filings are read from the same SEC hosts under the same policy.
        "nport": CapacityDeclaration(
            calls_per_window=8,
            window_seconds=1,
            daily_budget=5000,
            documented="SEC EDGAR fair access (same hosts as sec): max 10 requests/second",
        ),
        # Twelve Data's plan: 8 API credits per minute and 800 per day; `/eod` and
        # `/time_series` cost one credit each. Split per environment (above).
        "twelvedata": CapacityDeclaration(
            calls_per_window=8,
            window_seconds=60,
            daily_budget=800,
            environment_shares=TWELVE_DATA_ENVIRONMENT_SHARES,
            documented="Twelve Data Basic (free) plan: 8 API credits/minute, 800/day; one key for both environments",
        ),
        # OpenFIGI v3 with an API key: 25 requests per 6 seconds (100 jobs each); no
        # daily limit is published, so the seat is paced, not budgeted.
        "openfigi": CapacityDeclaration(
            calls_per_window=25,
            window_seconds=6,
            documented="OpenFIGI v3 keyed: 25 requests/6 s, 100 jobs/request; no daily limit",
        ),
        # moomoo OpenAPI rate-limits quote endpoints at 60 requests per 30 s EACH and has
        # no call quota (init.md §5, 2026-07-10). `moomoo_ledger` paces 8 per 30 s across
        # every endpoint — conservative, and the pace the origins actually see — and keeps
        # MOOMOO_MONTHLY_CALL_BUDGET as its own runaway backstop.
        "moomoo": CapacityDeclaration(
            calls_per_window=8,
            window_seconds=30,
            documented="moomoo OpenAPI: 60 requests/30 s per quote endpoint; no call quota",
        ),
        # Yahoo's chart endpoint publishes no limit and has no SLA (init.md §9); the
        # ceiling is ours. 2,000/day is 13x production's busiest day — room for a forced
        # re-fetch of every universe with the executor's three attempts per cell.
        "yahoo": CapacityDeclaration(
            calls_per_window=2,
            window_seconds=1,
            daily_budget=2000,
            documented="none published (unofficial endpoint, no SLA)",
        ),
        # Nasdaq's index-constituent endpoint publishes no limit; the weekly refresh
        # makes one call.
        "nasdaq-index": CapacityDeclaration(
            calls_per_window=1,
            window_seconds=1,
            daily_budget=50,
            documented="none published",
        ),
        # The model and search seats are provider-agnostic placeholders sized for a QQQ
        # backfill; a provider's real limits replace them in the PR that provisions it
        # (#70, #732).
        "filing-extraction-model": CapacityDeclaration(
            calls_per_window=30,
            window_seconds=60,
            daily_budget=2000,
            documented="placeholder seat (#70)",
        ),
        "search": CapacityDeclaration(
            calls_per_window=20,
            window_seconds=60,
            daily_budget=500,
            documented="placeholder seat (#732)",
        ),
    }
)

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
                ledger_seat="yahoo",
            ),
            OriginRegistration(
                origin_source="twelve-data:v1",
                origin_id="origin:twelve-data:v1",
                value_key=TWELVE_DATA_VALUE_KEY,
                parser_versions=TWELVE_DATA_PARSER_VERSION_HISTORY,
                # One key shared by both environments: the seat's split is declared above.
                ledger_seat="twelvedata",
            ),
            OriginRegistration(
                origin_source="twelve-data:v1",
                origin_id="origin:twelve-data:v1",
                # The v1 parser wrote the value under `price` (#545's historical entry).
                value_key="price",
                parser_versions=("twelve-data-parser:v1",),
                ledger_seat="twelvedata",
            ),
            # Third origin: moomoo OpenD's daily regular-session close, session-bound like
            # Twelve Data (the settled session at/before the price cutoff, never the
            # in-progress bar). Enabled per environment by MOOMOO_KLINE_ORIGIN_ENABLED.
            OriginRegistration(
                origin_source=f"{MOOMOO_KLINE_ORIGIN}:v1",
                origin_id=f"origin:{MOOMOO_KLINE_ORIGIN}:v1",
                value_key=MOOMOO_KLINE_VALUE_KEY,
                parser_versions=(MOOMOO_KLINE_PARSER_VERSION,),
                ledger_seat="moomoo",
            ),
        ),
        corroboration_class="A",
        session_bound=True,
        ledger_seat="yahoo",
        notes=(
            "Yahoo has no published quota; the second origin's capacity is the binding one.",
            "moomoo's historical-candlestick quota is 2,000 distinct stocks per rolling 30-day "
            "window (not calls); the governed universes use ~120 of them.",
        ),
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
        origins=(
            # Second origin: moomoo's vendor-normalized annual income statement and balance
            # sheet, value-reconciled per field against the XBRL facts under
            # `quality_report.FINANCIAL_FACT_RECONCILIATION_POLICY`. Enabled per environment
            # by MOOMOO_FINANCIALS_ORIGIN_ENABLED. The primary (company-facts) writes the
            # shared primary vintage, so it is identified by the registration itself.
            OriginRegistration(
                origin_source=f"{MOOMOO_FINANCIALS_ORIGIN}:v1",
                origin_id=f"origin:{MOOMOO_FINANCIALS_ORIGIN}:v1",
                value_key=MOOMOO_FINANCIALS_VALUE_KEY,
                parser_versions=(MOOMOO_FINANCIALS_PARSER_VERSION,),
                ledger_seat="moomoo",
            ),
        ),
        # Stays B until a scheduled staging tick shows the moomoo origin reconciling; the
        # plausibility oracle keeps grading every cell regardless of the second origin.
        corroboration_class="B",
        # SEC fair access; the seat's numbers are in `LEDGER_CAPACITIES`.
        ledger_seat="sec",
        notes=(
            "With MOOMOO_FINANCIALS_ORIGIN_ENABLED, revenue / net_income / gross_profit / "
            "total_assets are value-reconciled against moomoo's statements (class A per field).",
        ),
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


def source_by_parser(registrations: Sequence[SourceRegistration] = REGISTRATIONS) -> dict[str, tuple[str, str, str]]:
    """parser_version -> (origin_source, origin_id, value_key), over every registered
    origin's history, so the quality report recognises every vintage ever written.

    A vintage claimed by two origins with different coordinates would mis-attribute
    every historical observation written under it, silently; that is refused here,
    at import, rather than discovered in a quality report (review on #743)."""
    mapping: dict[str, tuple[str, str, str]] = {}
    for registration in registrations:
        for origin in registration.origins:
            for vintage in origin.parser_versions:
                coordinate = (origin.origin_source, origin.origin_id, origin.value_key)
                if vintage in mapping and mapping[vintage] != coordinate:
                    raise ValueError(
                        f"parser vintage {vintage!r} is claimed by two origins with different coordinates: "
                        f"{mapping[vintage]} and {coordinate} (#72)"
                    )
                mapping[vintage] = coordinate
    return mapping


SEMANTIC_TYPES: tuple[str, ...] = registered_semantic_types()
RELEASE_SEMANTICS: frozenset[str] = semantic_types_of("release-derived")
FRESHNESS_WINDOWS: dict[str, timedelta] = freshness_windows()
SOURCE_BY_PARSER: dict[str, tuple[str, str, str]] = source_by_parser()
