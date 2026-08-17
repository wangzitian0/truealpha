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

Adding an ETF is one `UniverseSource` entry (and later, one table row) — the
refresh job handles the rest. Membership changes over time by design: the
weekly refresh publishes a new version only when the mapping actually changed.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb
from truealpha_contracts import canonical_sha256
from truealpha_contracts.models import DataSource

from data_engine import raw_store
from data_engine.datahub.production_topt.universe_corpus import corpus_list_version

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
    """One ETF/index universe: where its constituents come from and what it is called."""

    etf: str  # e.g. "qqq"
    index_api: str  # the operator's constituents endpoint
    universe_prefix: str  # e.g. "qqq-us" -> universe:qqq-us-<report_date>
    label: str
    mic: str = "xnas"

    @property
    def head_kind(self) -> str:
        return f"universe-list:{self.etf}"


UNIVERSE_SOURCES: dict[str, UniverseSource] = {
    "qqq": UniverseSource(
        etf="qqq",
        index_api="https://api.nasdaq.com/api/quote/list-type/nasdaq100",
        universe_prefix="qqq-us",
        label="Nasdaq-100 (QQQ) constituents, from the index operator's API",
    ),
}


def _get(url: str, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    """Status-honest fetch: an HTTP error's body IS the vendor's answer (#557)."""
    request = urllib.request.Request(url, headers=headers or _BROWSER_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return response.status, bytes(response.read())
    except urllib.error.HTTPError as error:
        with error:
            return error.code, bytes(error.read())


def parse_nasdaq_index_rows(body: bytes) -> list[dict[str, str]]:
    """The operator API's constituent rows: [{'ticker', 'name'}, ...], validated."""
    payload = json.loads(body)
    rows = (payload.get("data") or {}).get("data", {}).get("rows") or (payload.get("data") or {}).get("rows") or []
    out = [
        {"ticker": str(row["symbol"]).strip().upper(), "name": str(row.get("companyName", "")).strip()}
        for row in rows
        if row.get("symbol")
    ]
    if len(out) < 90:  # NDX is ~100-102; a short list is a source failure, not a small index
        raise ValueError(f"index constituents response yielded only {len(out)} rows")
    tickers = [row["ticker"] for row in out]
    if len(set(tickers)) != len(tickers):
        raise ValueError("index constituents response contains duplicate tickers")
    return out


def _resolve_figis(tickers: list[str], *, api_key: str = "") -> dict[str, str]:
    """Share-class FIGIs via OpenFIGI mapping, batched within the anonymous limits."""
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
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            results = json.load(response)
        for job, result in zip(chunk, results):
            data = result.get("data") or []
            if data:
                resolved[job["idValue"]] = str(data[0].get("shareClassFIGI") or data[0]["figi"]).lower()
        if not api_key:
            time.sleep(3)  # 25 req/min anonymous ceiling, with margin
    return resolved


def refresh_etf_constituents(
    connection: Connection[Any],
    source: UniverseSource,
    *,
    as_of: date | None = None,
    openfigi_api_key: str = "",
) -> int:
    """Fetch, land (bytes + rows), resolve identifiers. Returns rows landed.

    `knowable_at`/`as_of` are the fetch's own clock: a constituents list is an
    assertion about the index NOW; history accumulates as append-only rows.
    """
    fetched_at = datetime.now(UTC)
    as_of = as_of or fetched_at.date()
    status, body = _get(source.index_api)
    if status != 200:
        raise RuntimeError(f"{source.etf} constituents endpoint answered {status}: {body[:120]!r}")
    rows = parse_nasdaq_index_rows(body)

    fetch_id = raw_store.insert_fetch(
        connection,
        source=DataSource.NASDAQ_INDEX,
        source_record_id=f"index-constituents:{source.etf}:{as_of.isoformat()}",
        body=body,
        content_type="application/json",
        fetched_at=fetched_at,
        recorded_at=fetched_at,
    )

    from data_engine.sources import sec

    cik_index = sec.ticker_cik_index()
    figis = _resolve_figis([row["ticker"] for row in rows], api_key=openfigi_api_key)
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
                (etf_symbol, as_of, source, raw_fetch_id, ticker, company_name, cik, figi, knowable_at)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                source.etf,
                as_of,
                DataSource.NASDAQ_INDEX.value,
                fetch_id,
                row["ticker"],
                row["name"],
                cik_index[row["ticker"].replace(".", "-")],
                figis[row["ticker"]],
                fetched_at,
            ),
        )
    return len(rows)


def build_denominator(connection: Connection[Any], source: UniverseSource, *, report_date: date) -> dict[str, Any]:
    """The newest landed constituents snapshot, as a self-pinned denominator dict."""
    rows = connection.execute(
        """
        select ticker, cik, figi from staging.etf_constituent_facts
        where etf_symbol = %s and as_of = (
            select max(as_of) from staging.etf_constituent_facts where etf_symbol = %s
        )
        order by ticker
        """,
        (source.etf, source.etf),
    ).fetchall()
    if not rows:
        raise LookupError(f"no landed constituents for {source.etf}; run the refresh first")
    instruments = [
        [
            f"issuer:cik:{int(cik):010d}",
            f"security:figi:{figi}",
            f"listing:{source.mic}:{ticker.lower()}",
            ticker,
        ]
        for ticker, cik, figi in rows
    ]
    issuer_ids = {row[0] for row in instruments}
    return {
        "universe_id": f"universe:{source.universe_prefix}-{report_date.isoformat()}",
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
    }


def current_head_mapping_sha(connection: Connection[Any], source: UniverseSource) -> str | None:
    row = connection.execute(
        """
        select object.payload->>'instrument_mapping_sha256'
        from staging.accepted_ruleset_head head
        join staging.contract_objects object on object.contract_id = head.contract_id
        where head.kind = %s
        """,
        (source.head_kind,),
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
        (contract_id, source.head_kind, content_sha256, Jsonb(denominator)),
    )
    sequence = int(
        connection.execute(
            "select coalesce(max(sequence), 0) + 1 from staging.accepted_rulesets where kind = %s",
            (source.head_kind,),
        ).fetchone()[0]
    )
    connection.execute(
        "insert into staging.accepted_rulesets (kind, contract_id, sequence, note) values (%s, %s, %s, %s)",
        (source.head_kind, contract_id, sequence, note),
    )
    return contract_id, sequence


def resolve_universe_corpus(connection: Connection[Any], head_kind: str) -> dict[str, Any]:
    """The governed universe head as a corpus dict the composition root consumes.

    Fails loud when no head is published: capturing an unpublished universe would
    be scope from nowhere (#532's silent-fallback lesson, inverted on purpose).
    """
    row = connection.execute(
        """
        select object.payload
        from staging.accepted_ruleset_head head
        join staging.contract_objects object on object.contract_id = head.contract_id
        where head.kind = %s
        """,
        (head_kind,),
    ).fetchone()
    if row is None:
        raise LookupError(f"no published universe head for {head_kind}; publish one from the constituent plane")
    corpus = {"topt_denominator": row[0]}
    corpus_list_version(corpus)  # self-pin validation: drift refuses the load
    return corpus
