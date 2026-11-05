"""ETF/universe constituents as a captured, governed data plane (#539).

Universe membership used to be frozen corpus configuration compiled into the
image — adding an ETF meant writing code. It is now data end to end:

1. the index operator's constituent API is fetched with status-honest HTTP and
   the verbatim bytes land content-addressed in object storage (`raw.fetches`,
   the same lineage every other source gets);
2. parsed rows land append-only in ``staging.etf_constituent_facts`` with CIK
   (SEC's own ticker index) and FIGI (OpenFIGI, an open standard — CUSIP is
   licensed and deliberately absent) resolved as data, not hand-entered;
3. a list version is PUBLISHED from the plane: the denominator dict lands in
   ``staging.contract_objects`` and the governed head advances in
   ``staging.accepted_rulesets`` under ``kind='universe-list:<etf>'`` — the
   #532 pattern, so which universe a run captured is always resolvable to a
   content-addressed, raw-lineaged artifact;
4. the capture pipeline resolves its universe FROM the governed head
   (`resolve_universe_corpus`), never from a checked-in file.

Adding an ETF is one universe record in `environment_contract.v1.json` — the
refresh job handles the rest. Membership changes over time by design: the
weekly refresh publishes a new version only when the mapping actually changed.
"""

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
from data_engine.datahub.production_topt.universe_corpus import TOPT_CORPUS_FILENAME, corpus_list_version, load_corpus
from data_engine.sources import gateway

log = logging.getLogger(__name__)

_TUPLE_FIELDS = ["issuer_id", "instrument_id", "listing_id", "ticker"]
_BROWSER_HEADERS = {
    # The Nasdaq API answers plain clients with 403; a browser-shaped UA is the
    # documented community workaround and the response is public data.
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0",
    "Accept": "application/json",
    "Accept-Language": "en-US",
}


@dataclass(frozen=True)
class UniverseSource:
    """One ETF/index universe: where its constituents come from and what it is called.

    Built from the universe record (`truealpha_contracts.universes`); no field is stated here. The
    head kind is `lane_name(etf)` and the universe id starts with `record(etf).id_prefix`.
    """

    etf: str  # the key of the universe record, e.g. "qqq"
    index_api: str  # the operator's constituents endpoint ("" for a static-member source)
    label: str
    # The one exchange that lists EVERY member, or None for a multi-exchange source. A source
    # declares it only when its definition entails it (the Nasdaq-100 lists Nasdaq names; the
    # canary is a hand-picked Nasdaq list). A multi-exchange source (the S&P 500) leaves it None,
    # so each constituent resolves through the identity store or leaves the denominator (#1411 K6).
    mic: str | None
    # A hand-picked membership (#648 canary): the list itself is versioned operator
    # configuration and lands as the fetch body; CIK/FIGI still resolve through the
    # same live SEC/OpenFIGI paths as an index-fed source, so identity lineage is
    # identical either way. Empty means "fetch index_api".
    static_members: tuple[str, ...] = ()
    # The fund ticker to pull N-PORT holdings for (#63 first tranche) — None for
    # universes that are not a filing fund (the canary's static list has no N-PORT).
    nport_ticker: str | None = None


def universe_sources() -> dict[str, UniverseSource]:
    """The constituent plane of every universe record whose members an index API, N-PORT or a static list gives (#1522).

    A frozen corpus (TOPT) has no plane. The canary is a hand-picked list of five issuers, one per real code
    branch, all XNAS so the source-level MIC stays honest (#648): the post-deploy canary run drives the REAL
    pipeline over them and asserts named expectations.
    """
    sources: dict[str, UniverseSource] = {}
    for key, universe in load_environment_contract().universes.items():
        membership = universe.membership
        if membership.kind in ("index_api", "nport", "static"):
            sources[key] = UniverseSource(
                etf=key,
                index_api=membership.url or "",
                label=universe.label,
                mic=membership.mic,
                static_members=membership.members,
                nport_ticker=membership.nport_ticker,
            )
    return sources


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
    """Status-honest fetch: an HTTP error's body IS the vendor's answer (#557).

    Through the external call ledger (#729): the operator's answer, good or bad, is a
    recorded request before it is a landed row.
    """
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
    preferred = next((item for item in candidates if not str(item["ticker"]).strip()[:1].isdigit()), None)
    chosen = preferred or candidates[0]
    raw_ticker = str(chosen["ticker"]).strip()
    ticker = "" if raw_ticker[:1].isdigit() else raw_ticker.replace("/", ".").upper()
    figi = str(chosen.get("shareClassFIGI") or chosen["figi"]).lower()
    name = str(chosen.get("name") or "").strip()
    return ticker, figi, name


def _resolve_nport_identifiers(
    connection: Connection[Any],
    jobs: list[tuple[str, str]],
    *,
    as_of: date,
    api_key: str = "",
) -> dict[tuple[str, str], tuple[str, str, str]]:
    """Map N-PORT (id_type, id_value) pairs to (ticker, figi, name) via OpenFIGI, landing every batch in raw."""
    resolved: dict[tuple[str, str], tuple[str, str, str]] = {}
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-OPENFIGI-APIKEY"] = api_key
    batch_size = 100 if api_key else 10
    for start in range(0, len(jobs), batch_size):
        chunk = jobs[start : start + batch_size]
        payload = [
            {"idType": id_type, "idValue": id_val, "exchCode": "US", "includeUnlistedEquities": True}
            for id_type, id_val in chunk
        ]
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
                [f"{t}:{v}" for t, v in chunk],
                gateway.utc_day(),
            ),
            caller="universe_plane._resolve_nport_identifiers",
            timeout=30,
            raise_for_status=True,
        )
        fetched_at = datetime.now(UTC)
        raw_store.insert_fetch(
            connection,
            source=DataSource.OPENFIGI,
            source_record_id=f"figi-nport-mapping:{as_of.isoformat()}:{start}",
            body=body,
            content_type="application/json",
            fetched_at=fetched_at,
            recorded_at=fetched_at,
            metadata={"request_jobs": payload},
        )
        for key, result in zip(chunk, json.loads(body)):
            matched = _select_openfigi_match(result)
            if matched is not None:
                resolved[key] = matched
    return resolved


def _resolve_placeholder_ticker(h: Any, figi_name: str, title_tickers: dict[str, str]) -> str:
    """Resolve an unlisted OpenFIGI placeholder ticker against SEC's normalized title index."""
    from data_engine.sources.sec import normalize_company_title

    for candidate in (h.name, figi_name):
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
    for h in holdings:
        key = ("ID_ISIN", h.isin) if h.isin is not None else ("ID_CUSIP", str(h.cusip))
        mapped = resolved_ids.get(key)
        if mapped is None:
            unresolved_holdings.append(h.isin or str(h.cusip))
            continue
        ticker, figi, figi_name = mapped
        if not ticker and title_tickers:
            ticker = _resolve_placeholder_ticker(h, figi_name, title_tickers)
        if not ticker:
            unresolved_holdings.append(h.isin or str(h.cusip))
            continue
        weight = None if h.pct_val is None else Decimal(str(h.pct_val))
        display_name = (h.name or figi_name or ticker).strip()
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
) -> None:
    """Resolve missing CIKs via EDGAR fallback and raise if any constituent remains unresolved."""
    for ticker in tickers:
        normalized = ticker.replace(".", "-")
        if normalized not in cik_index:
            fallback = _resolve_cik_via_edgar(ticker, user_agent=user_agent)
            if fallback is not None:
                cik_index[normalized] = fallback
    missing_ciks = [ticker for ticker in tickers if ticker.replace(".", "-") not in cik_index]
    if missing_ciks:
        raise LookupError(f"unresolved identifiers for {etf}: {', '.join(sorted(missing_ciks))}")


def _load_sec_nport_indexes(
    http: Any, resolved_ids: dict[tuple[str, str], tuple[str, str, str]]
) -> tuple[dict[str, int], dict[str, str]]:
    """Load SEC ticker-to-CIK crosswalk and, when OpenFIGI returned a placeholder ticker, title-to-ticker index."""
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
                """
                select entity_id from staging.entity_aliases
                where scheme = 'legacy-id' and value = %s
                limit 1
                """,
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
    """Build ETF constituents from its latest SEC N-PORT-P filing (#1441)."""
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
    report_period_str = info.get("report_period") or filing_date
    effective_as_of = as_of or date.fromisoformat(report_period_str)

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
                (etf_symbol, as_of, source, raw_fetch_id, ticker, company_name, weight,
                 cik, figi, knowable_at)
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
    """A base-10 figure the operator wrote with thousands separators and a currency sign, as text.

    None when the operator wrote none or something else: a changed format reads as absent, never as a number.
    """
    text = str(raw if raw is not None else "").replace(",", "").strip()
    text = text.removeprefix(prefix).strip()
    return text if text.replace(".", "", 1).isdigit() else None


def parse_nasdaq_index_rows(body: bytes) -> list[ConstituentRow]:
    """The operator API's constituent rows: [{'ticker', 'name'}, ...], validated."""
    payload = json.loads(body)
    rows = (payload.get("data") or {}).get("data", {}).get("rows") or (payload.get("data") or {}).get("rows") or []
    out: list[ConstituentRow] = []
    for row in rows:
        if not row.get("symbol"):
            continue
        out.append(
            {
                "ticker": str(row["symbol"]).strip().upper(),
                "name": str(row.get("companyName", "")).strip(),
                # The operator's own per-constituent market cap; the official fund
                # WEIGHT is #63's N-PORT plane, never derived here.
                "market_cap": _decimal_text(row.get("marketCap")),
                # The operator quotes the last sale price beside every cap ("$86.60"); the pair
                # is the only outside witness of a listed unit count (#1523).
                "last_sale_price": _decimal_text(row.get("lastSalePrice"), prefix="$"),
            }
        )
    if len(out) < 90:  # NDX is ~100-102; a short list is a source failure, not a small index
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
    """Per-ticker authority fallback. SEC's own crosswalk files have gaps — AEP
    appears in neither company_tickers.json nor company_tickers_exchange.json
    (10,398 rows, verified 2026-08-17) while EDGAR itself resolves the ticker —
    so an unresolved ticker asks EDGAR directly rather than failing a
    102-listing universe on the index's clerical hole."""
    status, body = _get(
        f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&ticker={ticker}&type=10-K&output=atom&count=1",
        headers={"User-Agent": user_agent, "Accept": "application/atom+xml"},
        identity=FetchKey(DataSource.SEC.value, "browse-edgar", f"symbol:{ticker}", gateway.utc_day()),
        source=DataSource.SEC.value,
        endpoint="browse-edgar",
        caller="universe_plane._resolve_cik_via_edgar",
    )
    if status != 200:
        return None
    return _parse_edgar_cik(body)


def _resolve_figis(
    connection: Connection[Any],
    tickers: list[str],
    *,
    as_of: date,
    api_key: str = "",
) -> dict[str, str]:
    """Share-class FIGIs via OpenFIGI mapping, batched within the anonymous limits.

    Every response batch LANDS in raw before its mappings are used (#641 D3): the
    `security:figi:*` identities that key the whole universe plane must be traceable
    to the vendor bytes that asserted them — `DataSource.OPENFIGI`'s own contract
    comment says exactly this, and the resolver simply never did it.
    """
    resolved: dict[str, str] = {}
    jobs = [{"idType": "TICKER", "idValue": ticker, "exchCode": "US"} for ticker in tickers]
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-OPENFIGI-APIKEY"] = api_key
    batch_size = 100 if api_key else 10
    for start in range(0, len(jobs), batch_size):
        chunk = jobs[start : start + batch_size]
        request = urllib.request.Request(
            "https://api.openfigi.com/v3/mapping", data=json.dumps(chunk).encode(), headers=headers
        )
        # A recorded request (#729) whose 4xx raises: the mapping is identity-critical
        # and a partial answer must stop the refresh, not be silently parsed.
        _status, body = gateway.urlopen(
            DataSource.OPENFIGI.value,
            "mapping",
            request,
            identity=FetchKey.for_batch(
                DataSource.OPENFIGI.value, "mapping", tickers[start : start + batch_size], gateway.utc_day()
            ),
            caller="universe_plane._resolve_figis",
            timeout=30,
            raise_for_status=True,
        )
        fetched_at = datetime.now(UTC)
        raw_store.insert_fetch(
            connection,
            source=DataSource.OPENFIGI,
            source_record_id=f"figi-mapping:{as_of.isoformat()}:{start}",
            body=body,
            content_type="application/json",
            fetched_at=fetched_at,
            recorded_at=fetched_at,
            # OpenFIGI responses do not echo the query, so the response bytes alone
            # cannot say which FIGI belongs to which ticker — the request batch rides
            # in metadata (body stays vendor-verbatim) so the pairing is
            # reconstructible from the landed row (review on #643).
            metadata={"request_jobs": chunk},
        )
        results = json.loads(body)
        for job, result in zip(chunk, results):
            data = result.get("data") or []
            if data:
                resolved[job["idValue"]] = str(data[0].get("shareClassFIGI") or data[0]["figi"]).lower()
    return resolved


def refresh_etf_constituents(
    connection: Connection[Any],
    source: UniverseSource,
    *,
    as_of: date | None = None,
    openfigi_api_key: str = "",
    http: Any = None,
) -> int:
    """Fetch, land (bytes + rows), resolve identifiers. Returns rows landed.

    `knowable_at`/`as_of` are the fetch's own clock for index-API and static sources;
    for N-PORT-backed sources, `knowable_at` is the filing's own `filing_date` (#1441).
    """
    if not source.index_api and not source.static_members and source.nport_ticker:
        return _refresh_nport_constituents(
            connection, source, as_of=as_of, openfigi_api_key=openfigi_api_key, http=http
        )
    fetched_at = datetime.now(UTC)
    as_of = as_of or fetched_at.date()
    if source.static_members:
        from data_engine.datahub.resolve_coordinates import _resolve_issuer_display_name

        # The membership IS the operator's versioned configuration — land it as the
        # fetch body so the denominator's raw lineage points at exactly what was
        # decided, under DataSource.RELEASE like other release-derived assertions.
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
        # The entity is the operator's list type, the last segment of the endpoint ("nasdaq100").
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

    from data_engine.sources import sec

    cik_index = sec.ticker_cik_index()
    figis = _resolve_figis(connection, [row["ticker"] for row in rows], as_of=as_of, api_key=openfigi_api_key)
    from data_engine.config import settings as _settings

    for row in rows:
        normalized = row["ticker"].replace(".", "-")
        if normalized not in cik_index:
            fallback = _resolve_cik_via_edgar(row["ticker"], user_agent=_settings.sec_user_agent)
            if fallback is not None:
                cik_index[normalized] = fallback
    missing = [
        row["ticker"] for row in rows if row["ticker"].replace(".", "-") not in cik_index or row["ticker"] not in figis
    ]
    if missing:
        # Fail loud: publishing a universe with unresolved identities would push
        # the gap into every downstream cell. Resolution problems are fixed HERE.
        raise LookupError(f"unresolved identifiers for {source.etf}: {', '.join(sorted(missing))}")

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


#: The reason a constituent leaves the denominator: no source names the exchange that lists it.
UNRESOLVED_LISTING_EXCHANGE = "unresolved_listing_exchange"


def resolve_listing_mics(
    connection: Connection[Any],
    source: UniverseSource,
    tickers: Sequence[str],
    *,
    valid_at: date | None = None,
    known_at: datetime | None = None,
) -> dict[str, str | None]:
    """The primary exchange MIC (`xnas` vs `xnys`) of each ticker, or None when no source names it (#1411 K6).

    A multi-exchange index such as `sp500` contains both NYSE and Nasdaq listings. A MIC that no
    source asserted would mint a listing identity (`listing:xnys:aapl`) beside the real one
    (`listing:xnas:aapl`). So a ticker with no evidence gets None, and the caller excludes it by name.

    Evidence, from the weakest to the strongest (a stronger source overrides a weaker one):
    1. `source.mic`, when the source declares that one exchange lists every member.
    2. The governed TOPT corpus (`corpus.v1.json`) listing identities.
    3. Landed `qqq` constituents in `staging.etf_constituent_facts` (the Nasdaq-100 lists Nasdaq names).
    4. `staging.entity_aliases` where `scheme = 'mic-ticker'` (`<MIC>:<TICKER>`), the identity store.
       Only an alias that is valid on `valid_at`, as known at `known_at`, counts. An alias that
       has ended (`valid_to`) or has a retraction does not (`staging.entity_alias_valid_to`).
       Both default to now.
    A failed read raises. It never turns into a smaller denominator.
    """
    known_at = known_at or datetime.now(UTC)
    valid_at = valid_at or known_at.date()
    wanted = {ticker.upper() for ticker in tickers}
    mics: dict[str, str] = {}
    if source.mic is not None:
        mics.update(dict.fromkeys(wanted, source.mic))

    for row in load_corpus(TOPT_CORPUS_FILENAME)["topt_denominator"]["instruments"]:
        listing_id, ticker = str(row[2]), str(row[3]).upper()
        parts = listing_id.split(":")
        if ticker in wanted and len(parts) == 3 and parts[0] == "listing":
            mics[ticker] = parts[1].lower()

    if wanted:
        with connection.transaction():
            qqq_rows = connection.execute(
                """
                select distinct upper(ticker)
                from staging.etf_constituent_facts
                where etf_symbol = 'qqq' and upper(ticker) = any(%s)
                """,
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
    """The newest landed constituents snapshot, as a self-pinned denominator dict."""
    rows = connection.execute(
        """
        select ticker, cik, figi from staging.etf_constituent_facts
        where etf_symbol = %s and (as_of, knowable_at) = (
            -- The single newest REFRESH, not the union of a day's refreshes: rows
            -- of one refresh share their knowable_at, so a same-day re-run cannot
            -- smear two snapshots into one denominator (Copilot Medium on #606).
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
    # A constituent whose exchange no source names is not valued under a guessed exchange.
    # It leaves the denominator by name, and the payload shows it.
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
    """Land the denominator as a governed object and advance the universe head.

    The #532 publish shape: object + pointer in one call, because a published
    object nothing points at is inert and a pointer to nothing resolves nowhere.
    """
    denominator = build_denominator(connection, source, report_date=report_date)
    content_sha256 = canonical_sha256(denominator)
    contract_id = f"universe-list:{content_sha256}"
    connection.execute(
        "insert into staging.contract_objects (contract_id, contract_kind, content_sha256, payload) "
        "values (%s, %s, %s, %s) on conflict (contract_id) do nothing",
        (contract_id, lane_name(source.etf), content_sha256, Jsonb(denominator)),
    )
    sequence_row = connection.execute(
        "select coalesce(max(sequence), 0) + 1 from staging.accepted_rulesets where kind = %s",
        (lane_name(source.etf),),
    ).fetchone()
    assert sequence_row is not None
    sequence = int(sequence_row[0])
    connection.execute(
        "insert into staging.accepted_rulesets (kind, contract_id, sequence, note) values (%s, %s, %s, %s)",
        (lane_name(source.etf), contract_id, sequence, note),
    )
    return contract_id, sequence


def latest_quarter_end(as_of: date) -> date:
    """The most recent completed calendar quarter end at `as_of` — the report_date a
    scheduled refresh publishes under (the universe asserts scope for that partition)."""
    quarter_ends = [
        date(as_of.year - 1, 12, 31),
        date(as_of.year, 3, 31),
        date(as_of.year, 6, 30),
        date(as_of.year, 9, 30),
        date(as_of.year, 12, 31),
    ]
    return max(q for q in quarter_ends if q < as_of)


#: How many excluded tickers the summary line names. The payload and the output metadata hold all of them.
EXCLUDED_NAMES_SHOWN = 5


class RefreshOutcome(str):
    """The summary line of one refresh. It is a string for the log, and it carries the excluded constituents.

    A constituent whose listing exchange no source names leaves the denominator (#1411 K6). The line names
    the count and the first `EXCLUDED_NAMES_SHOWN` tickers, so the operator sees the exclusion where the
    run reports. `excluded` holds the same items as the `excluded` key of the denominator payload.
    """

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
    """One refresh cycle: fetch+land, then publish ONLY when membership changed.

    The single entrypoint the operator script and the scheduled job share — the
    owner requirement is automatic refresh (index membership and weights move
    over time), and automation must not spray a new governed version per run
    when nothing changed.
    """
    landed = refresh_etf_constituents(connection, source, openfigi_api_key=openfigi_api_key)
    fresh = build_denominator(connection, source, report_date=report_date)
    if fresh["instrument_mapping_sha256"] == current_head_mapping_sha(connection, source):
        return _summary(f"{source.etf}: {landed} rows landed; membership unchanged, head kept", fresh["excluded"])
    contract_id, sequence = publish_universe_list(connection, source, report_date=report_date, note=note)
    return _summary(
        f"{source.etf}: {landed} rows landed; published {contract_id} at sequence {sequence}", fresh["excluded"]
    )


def resolve_universe_corpus(connection: Connection[Any], head_kind: str) -> dict[str, Any]:
    """The governed universe head as a corpus dict the composition root consumes.

    Fails loud when no head is published: capturing an unpublished universe would
    be scope from nowhere (#532's silent-fallback lesson, inverted on purpose).

    Carries the head's own `recorded_at` as `published_at` (#530 item 2): identity and
    membership rows derived from this corpus are knowable when the universe was
    actually PUBLISHED, not merely when the run's partition starts. A sibling key next
    to `topt_denominator`, never inside it — `corpus_list_version`'s self-pin hash is
    computed over the denominator's own `instrument_tuple_fields`/`instruments`, so an
    added sibling cannot perturb it.
    """
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
    corpus_list_version(corpus)  # self-pin validation: drift refuses the load
    return corpus
