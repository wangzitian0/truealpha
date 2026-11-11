"""Per-source registrations and ledger seat capacities."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from importlib import import_module
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.environment_contract import DAY_SECONDS, load_environment_contract
from truealpha_contracts.strategy import RiskFreeInstrument, load_live_strategy_definition

from data_engine.datahub.production_topt.parser_identity import PARSER_VERSION_HISTORY

if TYPE_CHECKING:
    import psycopg

    from data_engine.datahub.production_topt.executor import SourceFetchPort


@dataclass(frozen=True)
class RouteCell:
    work_item_id: str
    semantic_type: str
    issuer_id: str
    instrument_id: str
    listing_id: str
    ticker: str


@dataclass(frozen=True)
class RouteContext:
    cutoff: datetime
    cutoff_date: date
    price_cutoff_date: date
    partition_start: datetime
    universe_published_at: datetime | None
    coordinates: Mapping[str, tuple[str, str, str, str]]
    connection: psycopg.Connection[Any] | None
    drill: Any | None = None
    raw_coordinates: Mapping[str, tuple[str, str, str, str]] | None = None
    universe_id: str | None = None


class RouteBuilder(Protocol):
    def __call__(self, context: RouteContext, cells: Sequence[RouteCell]) -> SourceFetchPort: ...


@dataclass(frozen=True)
class CapacityDeclaration:
    calls_per_window: int | None
    window_seconds: int | None
    daily_budget: int | None = None
    concurrency: int = 1
    account: str | None = None
    documented: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        if (self.calls_per_window is None) != (self.window_seconds is None):
            raise ValueError("capacity window_seconds and calls_per_window go together")
        if self.calls_per_window is None and self.account is None:
            raise ValueError("capacity calls_per_window must be positive")
        for name in ("calls_per_window", "window_seconds", "concurrency"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"capacity {name} must be positive")
        if self.daily_budget is not None and self.daily_budget <= 0:
            raise ValueError("a daily budget must be positive; declare None for an unbudgeted seat")


SOURCE_RELIABILITY_PRIORS: Mapping[str, Decimal] = MappingProxyType(
    {
        "release-derived": Decimal("1"),
        "sec": Decimal("0.98"),
        "sec-company-facts": Decimal("0.98"),
        "origin:sec-company-facts:v1": Decimal("0.98"),
        "sec-nport": Decimal("0.98"),
        "origin:sec-nport:v1": Decimal("0.98"),
        "nasdaq-index": Decimal("0.98"),
        "origin:nasdaq-index:v1": Decimal("0.98"),
        "us-treasury": Decimal("0.98"),
        "us-treasury-par-yield": Decimal("0.98"),
        "moomoo": Decimal("0.96"),
        "moomoo-kline:v1": Decimal("0.96"),
        "origin:moomoo-kline:v1": Decimal("0.96"),
        "moomoo-financials:v1": Decimal("0.96"),
        "origin:moomoo-financials:v1": Decimal("0.96"),
        "origin:moomoo-ratings:v1": Decimal("0.96"),
        "twelvedata": Decimal("0.95"),
        "twelve-data": Decimal("0.95"),
        "twelve-data:v1": Decimal("0.95"),
        "origin:twelve-data:v1": Decimal("0.95"),
        "yahoo": Decimal("0.90"),
        "yahoo-chart": Decimal("0.90"),
        "yahoo-chart:v1": Decimal("0.90"),
        "origin:yahoo:v1": Decimal("0.90"),
        "sec-10k": Decimal("0.88"),
        "origin:sec-10k:v1": Decimal("0.88"),
        "10k-extraction": Decimal("0.88"),
        "manual-review": Decimal("0.88"),
    }
)


def source_reliability_prior(identifier: str) -> Decimal | None:
    return SOURCE_RELIABILITY_PRIORS.get(identifier)


def _assign_reliability_prior(
    owner: OriginRegistration | SourceRegistration | MacroSourceRegistration,
    *candidates: str | None,
) -> None:
    declared: Decimal | None = None
    matched_key: str | None = None
    for key in candidates:
        if key is not None and key in SOURCE_RELIABILITY_PRIORS:
            declared = SOURCE_RELIABILITY_PRIORS[key]
            matched_key = key
            break
    supplied = owner.reliability_prior
    if supplied is not None and (supplied < Decimal("0") or supplied > Decimal("1")):
        raise ValueError(f"{candidates[0]}: reliability_prior must be in [0, 1], got {supplied}")
    if declared is not None:
        if supplied is not None and supplied != declared:
            raise ValueError(
                f"{candidates[0]}: reliability_prior {supplied} contradicts {matched_key!r} declaration {declared}"
            )
        object.__setattr__(owner, "reliability_prior", declared)


class AsOfAxis(StrEnum):
    SETTLED_SESSION = "settled_session"
    ACCESSION = "accession"
    RELEASE = "release"
    BUSINESS_DAY = "business_day"


def knowable_instant(axis: AsOfAxis | str, day: date) -> datetime:
    resolved = AsOfAxis(axis)
    if resolved is AsOfAxis.SETTLED_SESSION:
        from data_engine.datahub.market_prices import xnys_session_close_utc

        return xnys_session_close_utc(day)
    if resolved is AsOfAxis.ACCESSION:
        return datetime.combine(day, time.max, tzinfo=UTC)
    if resolved is AsOfAxis.BUSINESS_DAY:
        return datetime.combine(day + timedelta(days=1), time.min, tzinfo=UTC)
    return datetime.combine(day, time.min, tzinfo=UTC)


def last_settled_accession_date(cutoff: datetime) -> date:
    instant = cutoff.astimezone(UTC)
    day = instant.date()
    return day if knowable_instant(AsOfAxis.ACCESSION, day) <= instant else day - timedelta(days=1)


def _validate_as_of_axis(raw: AsOfAxis | str | None, owner_name: str, *, allow_none: bool = False) -> AsOfAxis | None:
    if raw is None and allow_none:
        return None
    if not raw:
        raise ValueError(f"{owner_name}: registration must declare as_of_axis (#1411 K1)")
    try:
        return AsOfAxis(raw)
    except ValueError as err:
        raise ValueError(f"{owner_name}: invalid as_of_axis {raw!r}; expected one of {tuple(AsOfAxis)}") from err


@dataclass(frozen=True)
class OriginRegistration:
    origin_source: str
    origin_id: str
    value_key: str
    parser_versions: tuple[str, ...]
    capacity: CapacityDeclaration | None = None
    ledger_seat: str | None = None
    per_key_cost: int | None = None
    as_of_axis: AsOfAxis | str | None = None
    reliability_prior: Decimal | None = None

    def __post_init__(self) -> None:
        _seat_capacity(self, self.origin_source)
        _assign_reliability_prior(self, self.origin_id, self.origin_source, self.ledger_seat)
        if self.per_key_cost is not None and self.per_key_cost <= 0:
            raise ValueError(f"{self.origin_source}: per_key_cost must be positive, got {self.per_key_cost}")
        object.__setattr__(
            self,
            "as_of_axis",
            _validate_as_of_axis(self.as_of_axis, self.origin_source, allow_none=True),
        )


def _seat_capacity(owner: OriginRegistration | SourceRegistration, name: str) -> None:
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
    payload: dict[str, Any] = {
        "calls_per_window": capacity.calls_per_window,
        "window_seconds": capacity.window_seconds,
        "daily_budget": capacity.daily_budget,
        "concurrency": capacity.concurrency,
    }
    if capacity.account is not None:
        contract = load_environment_contract()
        payload["account"] = capacity.account
        payload["shares"] = {
            environment: {
                limit.limit_id: limit.share for limit in contract.account_share(capacity.account, environment).limits
            }
            for environment in contract.vendor_accounts.default_percent
        }
    return payload


@dataclass(frozen=True)
class SourceRegistration:
    source_id: str
    version: str
    semantic_types: tuple[str, ...]
    freshness_max_age: Mapping[str, timedelta]
    route_builder: str
    origins: tuple[OriginRegistration, ...] = ()
    corroboration_class: str = "B"
    capacity: CapacityDeclaration | None = None
    ledger_seat: str | None = None
    per_key_cost: int = 0
    as_of_axis: AsOfAxis | str = AsOfAxis.RELEASE
    session_bound: bool = False
    reliability_prior: Decimal | None = None
    notes: tuple[str, ...] = field(default=(), compare=False)

    def __post_init__(self) -> None:
        _seat_capacity(self, self.source_id)
        _assign_reliability_prior(self, self.source_id, self.ledger_seat)
        if self.ledger_seat is not None and self.per_key_cost <= 0:
            raise ValueError(f"{self.source_id}: vendor registration must declare positive per_key_cost (#1411 K1)")
        object.__setattr__(
            self,
            "as_of_axis",
            _validate_as_of_axis(self.as_of_axis, self.source_id),
        )

    def entry_payload(self) -> dict[str, Any]:
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
                    "per_key_cost": o.per_key_cost,
                    "as_of_axis": o.as_of_axis,
                    "reliability_prior": str(o.reliability_prior) if o.reliability_prior is not None else None,
                }
                for o in self.origins
            ],
            "corroboration_class": self.corroboration_class,
            "capacity": _capacity_payload(self.capacity),
            "ledger_seat": self.ledger_seat,
            "per_key_cost": self.per_key_cost,
            "as_of_axis": self.as_of_axis,
            "session_bound": self.session_bound,
            "reliability_prior": str(self.reliability_prior) if self.reliability_prior is not None else None,
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


MACRO_AS_OF_AXES: tuple[AsOfAxis, ...] = (AsOfAxis.BUSINESS_DAY,)


@dataclass(frozen=True)
class MacroSeries:
    series_id: str
    label: str
    source_column: str
    unit: str = "annualized_decimal_fraction"

    def __post_init__(self) -> None:
        for name in ("series_id", "label", "source_column", "unit"):
            if not getattr(self, name).strip():
                raise ValueError(f"macro series {name} must not be empty")


@dataclass(frozen=True)
class MacroSourceRegistration:
    source_id: str
    version: str
    series: tuple[MacroSeries, ...]
    ledger_seat: str
    per_key_cost: int
    as_of_axis: AsOfAxis | str
    max_age_days: int
    first_year: int
    capacity: CapacityDeclaration | None = None
    reliability_prior: Decimal | None = None
    notes: tuple[str, ...] = field(default=(), compare=False)

    def __post_init__(self) -> None:
        _seat_capacity(self, self.source_id)  # type: ignore[arg-type]
        _assign_reliability_prior(self, self.source_id, self.ledger_seat)
        if self.ledger_seat not in LEDGER_CAPACITIES:
            raise ValueError(f"{self.source_id}: ledger seat {self.ledger_seat!r} has no capacity declaration")
        if self.per_key_cost <= 0:
            raise ValueError(f"{self.source_id}: macro registration must declare positive per_key_cost (#1411 K1)")
        validated_axis = _validate_as_of_axis(self.as_of_axis, self.source_id)
        if validated_axis not in MACRO_AS_OF_AXES:
            raise ValueError(f"{self.source_id}: as_of_axis {self.as_of_axis!r} is not one of {MACRO_AS_OF_AXES}")
        object.__setattr__(self, "as_of_axis", validated_axis)
        if not self.series:
            raise ValueError(f"{self.source_id}: a macro registration must declare at least one series")
        ids = [item.series_id for item in self.series]
        if len(ids) != len(set(ids)):
            raise ValueError(f"{self.source_id}: a series id appears twice: {ids}")
        if self.max_age_days <= 0:
            raise ValueError(f"{self.source_id}: max_age_days must be positive, got {self.max_age_days}")


TWELVE_DATA_ORIGIN = "twelve-data"
TWELVE_DATA_PARSER_VERSION_HISTORY = ("twelve-data-parser:v2", "twelve-data-parser:v3")
TWELVE_DATA_PARSER_VERSION = TWELVE_DATA_PARSER_VERSION_HISTORY[-1]
TWELVE_DATA_MAPPING_VERSION = "twelve-data-map:v3"
TWELVE_DATA_VALUE_KEY = "close"

MOOMOO_KLINE_ORIGIN = "moomoo-kline"
MOOMOO_KLINE_PARSER_VERSION = "moomoo-kline-parser:v2"
MOOMOO_KLINE_MAPPING_VERSION = "moomoo-kline-map:v1"
MOOMOO_KLINE_VALUE_KEY = "close"
MOOMOO_FINANCIALS_ORIGIN = "moomoo-financials"
MOOMOO_FINANCIALS_PARSER_VERSION = "moomoo-financials-parser:v1"
MOOMOO_FINANCIALS_MAPPING_VERSION = "moomoo-financials-map:v1"
MOOMOO_FINANCIALS_VALUE_KEY = "revenue"


def _declaration(seat: str) -> CapacityDeclaration:
    contract = load_environment_contract()
    name = contract.account_of_seat(seat)
    row = contract.vendor_accounts.accounts[name]
    requests = [limit for limit in row.limits if limit.unit == "request"]
    figures = [
        (limit, limit.amount, limit.window_seconds)
        for limit in requests
        if limit.amount is not None and limit.window_seconds is not None
    ]
    if not figures and requests and all(limit.evidence.kind == "none_published" for limit in requests):
        return CapacityDeclaration(
            calls_per_window=None,
            window_seconds=None,
            account=name,
            documented=f"vendor account {name}: none_published; per-environment pace in model_seat.environments (#1520)",
        )
    rates = [figure for figure in figures if figure[2] != DAY_SECONDS]
    days = [figure for figure in figures if figure[2] == DAY_SECONDS]
    if len(rates) != 1 or len(days) > 1:
        raise ValueError(
            f"vendor account {name!r}: the gate paces one rate window and one daily budget, "
            f"the row holds {[figure[0].id for figure in figures]}"
        )
    ((_, calls, window),) = rates
    return CapacityDeclaration(
        calls_per_window=calls,
        window_seconds=window,
        daily_budget=days[0][1] if days else None,
        account=name,
        documented=f"vendor account {name}: "
        + "; ".join(
            f"{limit.id} {amount} per {seconds} s ({limit.evidence.kind})" for limit, amount, seconds in figures
        ),
    )


def _ledger_capacities() -> Mapping[str, CapacityDeclaration]:
    contract = load_environment_contract()
    return MappingProxyType({seat: _declaration(seat) for seat in contract.vendor_seats})


LEDGER_CAPACITIES: Mapping[str, CapacityDeclaration] = _ledger_capacities()

REGISTRATIONS: tuple[SourceRegistration, ...] = (
    SourceRegistration(
        source_id="yahoo-chart",
        version="v1",
        semantic_types=("market-price",),
        freshness_max_age={"market-price": timedelta(days=5)},
        route_builder="data_engine.datahub.production_topt.market_price_adapter:build_route",
        origins=(
            OriginRegistration(
                origin_source="yahoo-chart:v1",
                origin_id="origin:yahoo:v1",
                value_key="close",
                parser_versions=PARSER_VERSION_HISTORY,
                ledger_seat="yahoo",
                per_key_cost=1,
                as_of_axis="settled_session",
            ),
            OriginRegistration(
                origin_source="twelve-data:v1",
                origin_id="origin:twelve-data:v1",
                value_key=TWELVE_DATA_VALUE_KEY,
                parser_versions=TWELVE_DATA_PARSER_VERSION_HISTORY,
                ledger_seat="twelvedata",
                per_key_cost=2,
                as_of_axis="settled_session",
            ),
            OriginRegistration(
                origin_source="twelve-data:v1",
                origin_id="origin:twelve-data:v1",
                value_key="price",
                parser_versions=("twelve-data-parser:v1",),
                ledger_seat="twelvedata",
                per_key_cost=2,
                as_of_axis="settled_session",
            ),
            OriginRegistration(
                origin_source=f"{MOOMOO_KLINE_ORIGIN}:v1",
                origin_id=f"origin:{MOOMOO_KLINE_ORIGIN}:v1",
                value_key=MOOMOO_KLINE_VALUE_KEY,
                parser_versions=("moomoo-kline-parser:v1", MOOMOO_KLINE_PARSER_VERSION),
                ledger_seat="moomoo",
                per_key_cost=1,
                as_of_axis="settled_session",
            ),
        ),
        corroboration_class="A",
        session_bound=True,
        ledger_seat="yahoo",
        per_key_cost=1,
        as_of_axis="settled_session",
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
        freshness_max_age={"listing-identity": timedelta(days=365), "universe-membership": timedelta(days=365)},
        route_builder="data_engine.datahub.production_topt.release_derived_adapter:build_route",
        corroboration_class="release",
        per_key_cost=0,
        as_of_axis="release",
    ),
    SourceRegistration(
        source_id="sec-company-facts",
        version="v1",
        semantic_types=("financial-fact",),
        freshness_max_age={
            "financial-fact": timedelta(days=load_live_strategy_definition().eligibility.maximum_input_age_days)
        },
        route_builder="data_engine.datahub.production_topt.sec_financial_adapter:build_route",
        origins=(
            OriginRegistration(
                origin_source=f"{MOOMOO_FINANCIALS_ORIGIN}:v1",
                origin_id=f"origin:{MOOMOO_FINANCIALS_ORIGIN}:v1",
                value_key=MOOMOO_FINANCIALS_VALUE_KEY,
                parser_versions=(MOOMOO_FINANCIALS_PARSER_VERSION,),
                ledger_seat="moomoo",
                per_key_cost=2,
                as_of_axis="settled_session",
            ),
        ),
        corroboration_class="B",
        ledger_seat="sec",
        per_key_cost=1,
        as_of_axis="accession",
        notes=(
            "With MOOMOO_FINANCIALS_ORIGIN_ENABLED, revenue / net_income / gross_profit / "
            "total_assets are value-reconciled against moomoo's statements (class A per field).",
        ),
    ),
)

MACRO_REGISTRATIONS: tuple[MacroSourceRegistration, ...] = (
    MacroSourceRegistration(
        source_id="us-treasury-par-yield",
        version="v1",
        series=(
            MacroSeries(
                series_id=RiskFreeInstrument.US_TREASURY_BILL_3_MONTH.value,
                label="U.S. Treasury 3-month par yield",
                source_column="3 Mo",
            ),
            MacroSeries(
                series_id="us_treasury_note_2_year",
                label="U.S. Treasury 2-year par yield",
                source_column="2 Yr",
            ),
            MacroSeries(
                series_id="us_treasury_note_10_year",
                label="U.S. Treasury 10-year par yield",
                source_column="10 Yr",
            ),
        ),
        ledger_seat="us-treasury",
        per_key_cost=1,
        as_of_axis="business_day",
        max_age_days=5,
        first_year=2000,
        notes=(
            "The Treasury obtains its input quotes at about 3:30 PM New York time on each business day. "
            "A first observation is knowable at 00:00 UTC of the next day. A revised value is knowable "
            "from the moment it was fetched.",
        ),
    ),
)


def macro_registration_for(series_id: str) -> MacroSourceRegistration:
    for registration in MACRO_REGISTRATIONS:
        if any(item.series_id == series_id for item in registration.series):
            return registration
    raise LookupError(f"no macro registration carries the series {series_id!r}")


def registered_semantic_types() -> tuple[str, ...]:
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


def origin_per_key_cost(origin_source: str, registrations: Sequence[SourceRegistration] = REGISTRATIONS) -> int:
    for registration in registrations:
        for origin in registration.origins:
            if origin.origin_source == origin_source and origin.per_key_cost is not None:
                return origin.per_key_cost
    raise LookupError(f"no registered origin named {origin_source!r} declares per_key_cost (#1411 K1)")


def uses_twelve_data(universe_id: str | None) -> bool:
    if not universe_id:
        return True
    from truealpha_contracts.environment_contract import UnknownUniverse, load_environment_contract

    contract = load_environment_contract()
    try:
        key = contract.universe_of_id(universe_id)
    except UnknownUniverse:
        return True
    universe = contract.record(key)
    return universe.price_series or universe.is_sentinel


def configured_origin_sources(semantic_type: str, *, universe_id: str | None = None) -> set[str]:
    from data_engine.config import settings

    reg = registration_for(semantic_type)
    required = {"primary"}
    if reg.source_id == "yahoo-chart":
        if settings.twelve_data_api_key and uses_twelve_data(universe_id):
            required.add(f"{TWELVE_DATA_ORIGIN}:v1")
        if settings.moomoo_kline_origin_enabled:
            required.add(f"{MOOMOO_KLINE_ORIGIN}:v1")
    elif reg.source_id == "sec-company-facts":
        if settings.moomoo_financials_origin_enabled:
            required.add(f"{MOOMOO_FINANCIALS_ORIGIN}:v1")
    return required
