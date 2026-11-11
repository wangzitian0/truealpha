"""ETF and index constituents as a captured, governed data plane (#539)."""

from __future__ import annotations

import json
import logging
import re
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, TypedDict

from psycopg import Connection
from psycopg.types.json import Jsonb
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.environment_contract import load_environment_contract
from truealpha_contracts.models import DataSource
from truealpha_contracts.universes import lane_name, record

from data_engine import raw_store
from data_engine.datahub.fetch_key import FetchKey
from data_engine.datahub.production_topt.concept_mapping import publish_governed_object
from data_engine.datahub.production_topt.universe_corpus import TOPT_CORPUS_FILENAME, corpus_list_version, load_corpus
from data_engine.sources import gateway

log = logging.getLogger(__name__)

_TUPLE_FIELDS = ["issuer_id", "instrument_id", "listing_id", "ticker"]
_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0",
    "Accept": "application/json",
    "Accept-Language": "en-US",
}
UNRESOLVED_LISTING_EXCHANGE = "unresolved_listing_exchange"
EXCLUDED_NAMES_SHOWN = 5


@dataclass(frozen=True)
class UniverseSource:
    """ETF or index universe constituent source definition."""

    etf: str
    index_api: str
    label: str
    mic: str | None
    static_members: tuple[str, ...] = ()
    nport_ticker: str | None = None


def universe_sources() -> dict[str, UniverseSource]:
    """Return all governed universe sources driven by index API, N-PORT, or static lists."""
    return {
        key: UniverseSource(
            etf=key,
            index_api=universe.membership.url or "",
            label=universe.label,
            mic=universe.membership.mic,
            static_members=universe.membership.members,
            nport_ticker=universe.membership.nport_ticker,
        )
        for key, universe in load_environment_contract().universes.items()
        if universe.membership.kind in ("index_api", "nport", "static")
    }


UNIVERSE_SOURCES: dict[str, UniverseSource] = universe_sources()


def _get(
    url: str,
    headers: dict[str, str] | None = None,
    *,
    identity: FetchKey,
    source: str = DataSource.NASDAQ_INDEX.value,
    endpoint: str = "constituents",
    caller: str = "universe_plane._get",
) -> tuple[int | None, bytes]:
    request = urllib.request.Request(url, headers=headers or _BROWSER_HEADERS)
    return gateway.urlopen(source, endpoint, request, identity=identity, caller=caller, timeout=30)


class ConstituentRow(TypedDict):
    ticker: str
    name: str
    market_cap: str | None
    last_sale_price: str | None


def _select_openfigi_match(result: dict[str, Any]) -> tuple[str, str, str] | None:
    """Pick the US equity mapping from an OpenFIGI result, zeroing out digit-leading placeholder tickers."""
    candidates = [
        item
        for item in (result.get("data") or [])
        if item.get("ticker") and (item.get("shareClassFIGI") or item.get("figi"))
    ]
    if not candidates:
        return None
    chosen = next((item for item in candidates if not str(item["ticker"]).strip()[:1].isdigit()), candidates[0])
    raw_ticker = str(chosen["ticker"]).strip()
    ticker = "" if raw_ticker[:1].isdigit() else raw_ticker.replace("/", ".").upper()
    figi = str(chosen.get("shareClassFIGI") or chosen["figi"]).lower()
    return ticker, figi, str(chosen.get("name") or "").strip()


def _post_openfigi_batches(
    connection: Connection[Any],
    items: Sequence[Any],
    *,
    build_job: Any,
    batch_token: Any,
    record_prefix: str,
    as_of: date,
    api_key: str,
    caller: str,
) -> list[tuple[Any, dict[str, Any]]]:
    headers = {"Content-Type": "application/json", **({"X-OPENFIGI-APIKEY": api_key} if api_key else {})}
    batch_size = 100 if api_key else 10
    paired: list[tuple[Any, dict[str, Any]]] = []
    for start in range(0, len(items), batch_size):
        chunk = items[start : start + batch_size]
        payload = [build_job(item) for item in chunk]
        request = urllib.request.Request(
            "https://api.openfigi.com/v3/mapping", data=json.dumps(payload).encode(), headers=headers
        )
        _status, body = gateway.urlopen(
            DataSource.OPENFIGI.value,
            "mapping",
            request,
            identity=FetchKey.for_batch(
                DataSource.OPENFIGI.value,
                "mapping",
                [batch_token(item) for item in chunk],
                gateway.utc_day(),
            ),
            caller=caller,
            timeout=30,
            raise_for_status=True,
        )
        fetched_at = datetime.now(UTC)
        raw_store.insert_fetch(
            connection,
            source=DataSource.OPENFIGI,
            source_record_id=f"{record_prefix}:{as_of.isoformat()}:{start}",
            body=body,
            content_type="application/json",
            fetched_at=fetched_at,
            recorded_at=fetched_at,
            metadata={"request_jobs": payload},
        )
        paired.extend(zip(chunk, json.loads(body)))
    return paired


def _resolve_nport_identifiers(
    connection: Connection[Any],
    jobs: list[tuple[str, str]],
    *,
    as_of: date,
    api_key: str = "",
) -> dict[tuple[str, str], tuple[str, str, str]]:
    """Map N-PORT `(id_type, id_value)` pairs to `(ticker, figi, name)` via OpenFIGI."""
    resolved: dict[tuple[str, str], tuple[str, str, str]] = {}
    paired = _post_openfigi_batches(
        connection,
        jobs,
        build_job=lambda pair: {
            "idType": pair[0],
            "idValue": pair[1],
            "exchCode": "US",
            "includeUnlistedEquities": True,
        },
        batch_token=lambda pair: f"{pair[0]}:{pair[1]}",
        record_prefix="figi-nport-mapping",
        as_of=as_of,
        api_key=api_key,
        caller="universe_plane._resolve_nport_identifiers",
    )
    for key, result in paired:
        matched = _select_openfigi_match(result)
        if matched is not None:
            resolved[key] = matched
    return resolved


def _resolve_placeholder_ticker(holding: Any, figi_name: str, title_tickers: dict[str, str]) -> str:
    from data_engine.sources.sec import normalize_company_title

    for candidate in (holding.name, figi_name):
        if candidate:
            matched = title_tickers.get(normalize_company_title(str(candidate)))
            if matched:
                return matched.replace("-", ".")
    return ""


def _aggregate_nport_holdings(
    holdings: Sequence[Any],
    resolved_ids: dict[tuple[str, str], tuple[str, str, str]],
    *,
    etf: str,
    title_tickers: dict[str, str] | None = None,
) -> dict[str, tuple[str, Decimal | None, str]]:
    """Collapse resolved N-PORT equity lines by ticker, summing weight across lines."""
    by_ticker: dict[str, tuple[str, Decimal | None, str]] = {}
    unresolved_holdings: list[str] = []
    for holding in holdings:
        key = ("ID_ISIN", holding.isin) if holding.isin is not None else ("ID_CUSIP", str(holding.cusip))
        mapped = resolved_ids.get(key)
        if mapped is None:
            unresolved_holdings.append(holding.isin or str(holding.cusip))
            continue
        ticker, figi, figi_name = mapped
        if not ticker and title_tickers:
            ticker = _resolve_placeholder_ticker(holding, figi_name, title_tickers)
        if not ticker:
            unresolved_holdings.append(holding.isin or str(holding.cusip))
            continue
        weight = None if holding.pct_val is None else Decimal(str(holding.pct_val))
        display_name = (holding.name or figi_name or ticker).strip()
        if ticker in by_ticker:
            prev_name, prev_weight, prev_figi = by_ticker[ticker]
            merged = (
                (prev_weight or Decimal(0)) + (weight or Decimal(0))
                if (prev_weight is not None or weight is not None)
                else None
            )
            by_ticker[ticker] = (prev_name, merged, prev_figi)
        else:
            by_ticker[ticker] = (display_name, weight, figi)
    if unresolved_holdings:
        raise LookupError(f"unresolved OpenFIGI holdings for {etf}: {', '.join(sorted(unresolved_holdings))}")
    return by_ticker


def _ensure_ciks_for_tickers(
    tickers: Sequence[str],
    cik_index: dict[str, int],
    *,
    etf: str,
    user_agent: str,
    figis: Mapping[str, str] | None = None,
) -> None:
    """Resolve missing CIKs via EDGAR fallback and raise if any constituent identifier remains unresolved."""
    for ticker in tickers:
        normalized = ticker.replace(".", "-")
        if normalized not in cik_index:
            fallback = _resolve_cik_via_edgar(ticker, user_agent=user_agent)
            if fallback is not None:
                cik_index[normalized] = fallback
    missing = [
        ticker
        for ticker in tickers
        if ticker.replace(".", "-") not in cik_index or (figis is not None and ticker not in figis)
    ]
    if missing:
        raise LookupError(f"unresolved identifiers for {etf}: {', '.join(sorted(missing))}")


def _load_sec_nport_indexes(
    http: Any, resolved_ids: dict[tuple[str, str], tuple[str, str, str]]
) -> tuple[dict[str, int], dict[str, str]]:
    from data_engine.sources import sec

    cik_index = sec.ticker_cik_index(http) if http is not None else sec.ticker_cik_index()
    if any(not ticker for ticker, _figi, _name in resolved_ids.values()):
        title_tickers = sec.title_ticker_index(http) if http is not None else sec.title_ticker_index()
    else:
        title_tickers = {}
    return cik_index, title_tickers


def _seed_sec_listing_mic_aliases(
    connection: Connection[Any],
    tickers: Sequence[str],
    *,
    mic_index: Mapping[str, str],
    valid_from: date,
    transaction_time: datetime,
) -> None:
    """Seed missing `mic-ticker` aliases in `staging.entity_aliases` from SEC's exchange crosswalk (#1418)."""
    if not tickers or not mic_index:
        return
    wanted = sorted({ticker.upper() for ticker in tickers})
    with connection.transaction():
        alias_rows = connection.execute(
            """
            select alias.value
            from staging.entity_aliases alias
            where alias.scheme = 'mic-ticker'
              and split_part(upper(alias.value), ':', 2) = any(%s)
              and alias.valid_from <= %s
              and coalesce(%s < staging.entity_alias_valid_to(alias.alias_id, %s), true)
              and alias.transaction_time <= %s
            """,
            (wanted, valid_from, valid_from, transaction_time, transaction_time),
        ).fetchall()
    existing_tickers = {str(val).partition(":")[2].upper() for (val,) in alias_rows if ":" in str(val)}
    for ticker in wanted:
        if ticker in existing_tickers:
            continue
        mic = (mic_index.get(ticker) or mic_index.get(ticker.replace(".", "-")) or "").strip().upper()
        if not mic:
            continue
        mic_ticker = f"{mic}:{ticker}"
        legacy_id = f"listing:{mic.lower()}:{ticker.lower()}"
        with connection.transaction():
            existing_entity = connection.execute(
                "select entity_id from staging.entity_aliases where scheme = 'legacy-id' and value = %s limit 1",
                (legacy_id,),
            ).fetchone()
            if existing_entity and existing_entity[0]:
                entity_id = existing_entity[0]
            else:
                minted = connection.execute(
                    "select staging.entity_mint('listing', 'legacy-id', %s, 'sec.company_tickers_exchange')",
                    (legacy_id,),
                ).fetchone()
                if not minted or not minted[0]:
                    continue
                entity_id = minted[0]
            for scheme, value in (("legacy-id", legacy_id), ("mic-ticker", mic_ticker)):
                connection.execute(
                    """
                    insert into staging.entity_aliases
                        (entity_id, scheme, value, valid_from, valid_to, transaction_time, source, raw_ref,
                         method, confidence, mapping_version)
                    values (%s, %s, %s, '-infinity', null, %s, %s, 'sec:company-tickers-exchange',
                            'asserted', %s, 'sec-tickers-exchange:v1')
                    on conflict do nothing
                    """,
                    (entity_id, scheme, value, transaction_time, DataSource.SEC.value, Decimal(1)),
                )


def _refresh_nport_constituents(
    connection: Connection[Any],
    source: UniverseSource,
    *,
    as_of: date | None,
    openfigi_api_key: str,
    http: Any = None,
) -> int:
    from data_engine.config import settings as _settings
    from data_engine.sources import nport, sec

    assert source.nport_ticker is not None
    fetched_at = datetime.now(UTC)
    client = http or sec.client()
    cik, series_id = nport.fund_series(client, source.nport_ticker)
    accession, filing_date = nport.latest_nport_accession(client, series_id)
    xml = nport.fetch_nport_xml(client, cik, accession)
    info, all_holdings = nport.parse_nport(xml)
    filing_dt = datetime.fromisoformat(filing_date).replace(tzinfo=UTC)
    effective_as_of = as_of or date.fromisoformat(info.get("report_period") or filing_date)

    fetch_id = raw_store.insert_fetch(
        connection,
        source=DataSource.NPORT,
        source_record_id=f"nport-constituents:{source.etf}:{accession}",
        body=xml,
        content_type="application/xml",
        fetched_at=fetched_at,
        recorded_at=fetched_at,
        source_published_at=filing_dt,
        metadata={"cik": cik, "series_id": series_id, "ticker": source.nport_ticker},
    )

    holdings = [h for h in all_holdings if h.asset_cat == "EC" and (h.isin is not None or h.cusip is not None)]
    if not holdings:
        raise ValueError(f"{source.etf} N-PORT {accession} yielded 0 equity holdings")

    job_keys = list(
        dict.fromkeys(("ID_ISIN", h.isin) if h.isin is not None else ("ID_CUSIP", str(h.cusip)) for h in holdings)
    )
    resolved_ids = _resolve_nport_identifiers(connection, job_keys, as_of=effective_as_of, api_key=openfigi_api_key)
    cik_index, title_tickers = _load_sec_nport_indexes(http, resolved_ids)
    by_ticker = _aggregate_nport_holdings(holdings, resolved_ids, etf=source.etf, title_tickers=title_tickers)
    _ensure_ciks_for_tickers(list(by_ticker), cik_index, etf=source.etf, user_agent=_settings.sec_user_agent)

    for ticker in sorted(by_ticker):
        name, weight, figi = by_ticker[ticker]
        connection.execute(
            """
            insert into staging.etf_constituent_facts
                (etf_symbol, as_of, source, raw_fetch_id, ticker, company_name, weight, cik, figi, knowable_at)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                source.etf,
                effective_as_of,
                DataSource.NPORT.value,
                fetch_id,
                ticker,
                name,
                weight,
                cik_index[ticker.replace(".", "-")],
                figi,
                filing_dt,
            ),
        )
    if source.mic is None:
        mic_index = sec.ticker_mic_index(http) if http is not None else sec.ticker_mic_index()
        _seed_sec_listing_mic_aliases(
            connection,
            sorted(by_ticker),
            mic_index=mic_index,
            valid_from=effective_as_of,
            transaction_time=filing_dt,
        )
    return len(by_ticker)


def _decimal_text(raw: object, *, prefix: str = "") -> str | None:
    text = str(raw if raw is not None else "").replace(",", "").strip().removeprefix(prefix).strip()
    return text if text.replace(".", "", 1).isdigit() else None


def parse_nasdaq_index_rows(body: bytes) -> list[ConstituentRow]:
    payload = json.loads(body)
    rows = (payload.get("data") or {}).get("data", {}).get("rows") or (payload.get("data") or {}).get("rows") or []
    out: list[ConstituentRow] = [
        {
            "ticker": str(row["symbol"]).strip().upper(),
            "name": str(row.get("companyName", "")).strip(),
            "market_cap": _decimal_text(row.get("marketCap")),
            "last_sale_price": _decimal_text(row.get("lastSalePrice"), prefix="$"),
        }
        for row in rows
        if row.get("symbol")
    ]
    if len(out) < 90:
        raise ValueError(f"index constituents response yielded only {len(out)} rows")
    tickers = [row["ticker"] for row in out]
    if len(set(tickers)) != len(tickers):
        raise ValueError("index constituents response contains duplicate tickers")
    return out


def _parse_edgar_cik(body: bytes) -> int | None:
    if b"<feed" in body and b"<company-info>" not in body:
        return None
    match = re.search(rb"<cik>(\d{4,10})</cik>", body) or re.search(rb"CIK=(\d{4,10})", body)
    return int(match.group(1)) if match else None


def _resolve_cik_via_edgar(ticker: str, *, user_agent: str) -> int | None:
    status, body = _get(
        f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&ticker={ticker}&type=10-K&output=atom&count=1",
        headers={"User-Agent": user_agent, "Accept": "application/atom+xml"},
        identity=FetchKey(DataSource.SEC.value, "browse-edgar", f"symbol:{ticker}", gateway.utc_day()),
        source=DataSource.SEC.value,
        endpoint="browse-edgar",
        caller="universe_plane._resolve_cik_via_edgar",
    )
    return _parse_edgar_cik(body) if status == 200 else None


def _resolve_figis(
    connection: Connection[Any],
    tickers: list[str],
    *,
    as_of: date,
    api_key: str = "",
) -> dict[str, str]:
    """Resolve share-class FIGIs via OpenFIGI, storing each response batch in `raw.fetches`."""
    resolved: dict[str, str] = {}
    paired = _post_openfigi_batches(
        connection,
        tickers,
        build_job=lambda ticker: {"idType": "TICKER", "idValue": ticker, "exchCode": "US"},
        batch_token=lambda ticker: ticker,
        record_prefix="figi-mapping",
        as_of=as_of,
        api_key=api_key,
        caller="universe_plane._resolve_figis",
    )
    for ticker, result in paired:
        data = result.get("data") or []
        if data:
            resolved[ticker] = str(data[0].get("shareClassFIGI") or data[0]["figi"]).lower()
    return resolved


def refresh_etf_constituents(
    connection: Connection[Any],
    source: UniverseSource,
    *,
    as_of: date | None = None,
    openfigi_api_key: str = "",
    http: Any = None,
) -> int:
    """Fetch, store raw bytes, resolve CIK and FIGI identifiers, and insert constituent facts."""
    if not source.index_api and not source.static_members and source.nport_ticker:
        return _refresh_nport_constituents(
            connection, source, as_of=as_of, openfigi_api_key=openfigi_api_key, http=http
        )
    fetched_at = datetime.now(UTC)
    as_of = as_of or fetched_at.date()
    if source.static_members:
        from data_engine.datahub.resolve_coordinates import _resolve_issuer_display_name

        rows: list[ConstituentRow] = [
            {
                "ticker": ticker,
                "name": _resolve_issuer_display_name(connection, ticker) or ticker,
                "market_cap": None,
                "last_sale_price": None,
            }
            for ticker in source.static_members
        ]
        body = json.dumps({"etf": source.etf, "members": list(source.static_members)}).encode()
        fetch_source = DataSource.RELEASE
    else:
        index_key = FetchKey(
            DataSource.NASDAQ_INDEX.value,
            "constituents",
            f"index:{source.index_api.rsplit('/', 1)[-1]}",
            gateway.utc_day(),
        )
        status, body = _get(source.index_api, identity=index_key, caller="universe_plane.refresh_etf_constituents")
        if status != 200:
            raise RuntimeError(f"{source.etf} constituents endpoint answered {status}: {body[:120]!r}")
        rows = parse_nasdaq_index_rows(body)
        fetch_source = DataSource.NASDAQ_INDEX

    fetch_id = raw_store.insert_fetch(
        connection,
        source=fetch_source,
        source_record_id=f"index-constituents:{source.etf}:{as_of.isoformat()}",
        body=body,
        content_type="application/json",
        fetched_at=fetched_at,
        recorded_at=fetched_at,
    )

    from data_engine.config import settings as _settings
    from data_engine.sources import sec

    tickers = [row["ticker"] for row in rows]
    cik_index = sec.ticker_cik_index()
    figis = _resolve_figis(connection, tickers, as_of=as_of, api_key=openfigi_api_key)
    _ensure_ciks_for_tickers(tickers, cik_index, etf=source.etf, user_agent=_settings.sec_user_agent, figis=figis)

    for row in rows:
        connection.execute(
            """
            insert into staging.etf_constituent_facts
                (etf_symbol, as_of, source, raw_fetch_id, ticker, company_name, market_cap, last_sale_price,
                 cik, figi, knowable_at)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                source.etf,
                as_of,
                DataSource.NASDAQ_INDEX.value,
                fetch_id,
                row["ticker"],
                row["name"],
                row.get("market_cap"),
                row.get("last_sale_price"),
                cik_index[row["ticker"].replace(".", "-")],
                figis[row["ticker"]],
                fetched_at,
            ),
        )
    return len(rows)


def resolve_listing_mics(
    connection: Connection[Any],
    source: UniverseSource,
    tickers: Sequence[str],
    *,
    valid_at: date | None = None,
    known_at: datetime | None = None,
) -> dict[str, str | None]:
    """Resolve primary exchange MIC (`xnas` vs `xnys`) per ticker, or `None` when unknown (#1411 K6)."""
    known_at = known_at or datetime.now(UTC)
    valid_at = valid_at or known_at.date()
    wanted = {ticker.upper() for ticker in tickers}
    mics: dict[str, str] = dict.fromkeys(wanted, source.mic) if source.mic is not None else {}

    for row in load_corpus(TOPT_CORPUS_FILENAME)["topt_denominator"]["instruments"]:
        listing_id, ticker = str(row[2]), str(row[3]).upper()
        parts = listing_id.split(":")
        if ticker in wanted and len(parts) == 3 and parts[0] == "listing":
            mics[ticker] = parts[1].lower()

    if wanted:
        with connection.transaction():
            qqq_rows = connection.execute(
                "select distinct upper(ticker) from staging.etf_constituent_facts "
                "where etf_symbol = 'qqq' and upper(ticker) = any(%s)",
                (sorted(wanted),),
            ).fetchall()
            alias_rows = connection.execute(
                """
                select alias.value
                from staging.entity_aliases alias
                where alias.scheme = 'mic-ticker'
                  and split_part(upper(alias.value), ':', 2) = any(%s)
                  and alias.valid_from <= %s
                  and coalesce(%s < staging.entity_alias_valid_to(alias.alias_id, %s), true)
                  and alias.transaction_time <= %s
                order by alias.transaction_time desc
                """,
                (sorted(wanted), valid_at, valid_at, known_at, known_at),
            ).fetchall()
        for (ticker,) in qqq_rows:
            mics[str(ticker).upper()] = "xnas"
        seen_alias: set[str] = set()
        for (alias_value,) in alias_rows:
            mic_part, _, ticker_part = str(alias_value).partition(":")
            ticker = ticker_part.upper()
            if mic_part and ticker and ticker not in seen_alias:
                seen_alias.add(ticker)
                mics[ticker] = mic_part.lower()

    return {ticker: mics.get(ticker.upper()) for ticker in tickers}


def build_denominator(connection: Connection[Any], source: UniverseSource, *, report_date: date) -> dict[str, Any]:
    """Build a self-pinned denominator dict from the newest landed constituent snapshot."""
    rows = connection.execute(
        """
        select ticker, cik, figi from staging.etf_constituent_facts
        where etf_symbol = %s and (as_of, knowable_at) = (
            select as_of, max(knowable_at) from staging.etf_constituent_facts
            where etf_symbol = %s and as_of = (
                select max(as_of) from staging.etf_constituent_facts where etf_symbol = %s
            )
            group by as_of
        )
        order by ticker
        """,
        (source.etf, source.etf, source.etf),
    ).fetchall()
    if not rows:
        raise LookupError(f"no landed constituents for {source.etf}; run the refresh first")
    mics = resolve_listing_mics(connection, source, [str(r[0]) for r in rows])
    instruments = [
        [
            f"issuer:cik:{int(cik):010d}",
            f"security:figi:{figi}",
            f"listing:{mics[ticker]}:{ticker.lower()}",
            ticker,
        ]
        for ticker, cik, figi in rows
        if mics[ticker] is not None
    ]
    excluded = [
        {"ticker": ticker, "issuer_id": f"issuer:cik:{int(cik):010d}", "reason": UNRESOLVED_LISTING_EXCHANGE}
        for ticker, cik, _figi in rows
        if mics[ticker] is None
    ]
    if excluded:
        log.warning(
            "%s: %d constituents have no known listing exchange and leave the denominator: %s",
            source.etf,
            len(excluded),
            ", ".join(item["ticker"] for item in excluded),
        )
    if not instruments:
        raise LookupError(f"no listing exchange resolved for any constituent of {source.etf}")
    issuer_ids = {row[0] for row in instruments}
    return {
        "universe_id": f"{record(source.etf).id_prefix}{report_date.isoformat()}",
        "list_version_id": "",
        "list_label": source.label,
        "accession": None,
        "report_date": report_date.isoformat(),
        "issuer_count": len(issuer_ids),
        "instrument_count": len(instruments),
        "obligation_count": len(instruments) * 4,
        "obligation_expansion": "four-semantics:v1",
        "instrument_tuple_fields": _TUPLE_FIELDS,
        "instruments": instruments,
        "instrument_mapping_sha256": canonical_sha256({"fields": _TUPLE_FIELDS, "instruments": instruments}),
        "identity_assertions": [],
        "excluded": excluded,
    }


def current_head_mapping_sha(connection: Connection[Any], source: UniverseSource) -> str | None:
    row = connection.execute(
        """
        select object.payload->>'instrument_mapping_sha256'
        from staging.accepted_ruleset_head head
        join staging.contract_objects object on object.contract_id = head.contract_id
        where head.kind = %s
        """,
        (lane_name(source.etf),),
    ).fetchone()
    return None if row is None else str(row[0])


def publish_universe_list(
    connection: Connection[Any], source: UniverseSource, *, report_date: date, note: str
) -> tuple[str, int]:
    """Store the denominator as a governed object and advance the universe head."""
    denominator = build_denominator(connection, source, report_date=report_date)
    content_sha256 = canonical_sha256(denominator)
    return publish_governed_object(
        connection,
        kind=lane_name(source.etf),
        contract_id=f"universe-list:{content_sha256}",
        content_sha256=content_sha256,
        payload=Jsonb(denominator),
        note=note,
    )


def latest_quarter_end(as_of: date) -> date:
    quarter_ends = (
        date(as_of.year - 1, 12, 31),
        date(as_of.year, 3, 31),
        date(as_of.year, 6, 30),
        date(as_of.year, 9, 30),
        date(as_of.year, 12, 31),
    )
    return max(q for q in quarter_ends if q < as_of)


class RefreshOutcome(str):
    """Summary string of one universe refresh carrying excluded constituent records."""

    excluded: tuple[dict[str, str], ...]

    def __new__(cls, text: str, excluded: Sequence[dict[str, str]] = ()) -> RefreshOutcome:
        outcome = super().__new__(cls, text)
        outcome.excluded = tuple(excluded)
        return outcome


def _summary(text: str, excluded: Sequence[dict[str, str]]) -> RefreshOutcome:
    if not excluded:
        return RefreshOutcome(text)
    names = ", ".join(item["ticker"] for item in excluded[:EXCLUDED_NAMES_SHOWN])
    return RefreshOutcome(
        f"{text}; {len(excluded)} constituents excluded, no listing exchange known "
        f"(first {min(len(excluded), EXCLUDED_NAMES_SHOWN)}: {names})",
        excluded,
    )


def refresh_and_publish(
    connection: Connection[Any],
    source: UniverseSource,
    *,
    report_date: date,
    note: str,
    openfigi_api_key: str = "",
) -> RefreshOutcome:
    """Refresh constituent facts and publish a new universe head only when membership changed."""
    landed = refresh_etf_constituents(connection, source, openfigi_api_key=openfigi_api_key)
    fresh = build_denominator(connection, source, report_date=report_date)
    if fresh["instrument_mapping_sha256"] == current_head_mapping_sha(connection, source):
        return _summary(f"{source.etf}: {landed} rows landed; membership unchanged, head kept", fresh["excluded"])
    contract_id, sequence = publish_universe_list(connection, source, report_date=report_date, note=note)
    return _summary(
        f"{source.etf}: {landed} rows landed; published {contract_id} at sequence {sequence}", fresh["excluded"]
    )


def resolve_universe_corpus(connection: Connection[Any], head_kind: str) -> dict[str, Any]:
    """Load the governed universe head as a validated corpus dict."""
    row = connection.execute(
        """
        select object.payload, object.recorded_at
        from staging.accepted_ruleset_head head
        join staging.contract_objects object on object.contract_id = head.contract_id
        where head.kind = %s
        """,
        (head_kind,),
    ).fetchone()
    if row is None:
        raise LookupError(f"no published universe head for {head_kind}; publish one from the constituent plane")
    payload, recorded_at = row
    corpus = {"topt_denominator": payload, "published_at": recorded_at.isoformat()}
    corpus_list_version(corpus)
    return corpus
