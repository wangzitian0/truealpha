"""OpenFIGI mapping client: ISIN -> listing records (venue + local ticker).

This is the ISIN/CUSIP resolution step CLAUDE.md's N-PORT gotcha calls for —
N-PORT identifies holdings by CUSIP/ISIN (foreign CUSIPs are placeholder zeros),
and moomoo/SEC need market-local codes.

Rate limits (docs, and observed 2026-07-11): without an API key 25 mapping
requests/min with 10 jobs each; with a free key 25 req/6s with 100 jobs each.
429s are expected at the keyless tier — handled by sleep-and-retry, so a full
~1,100-ISIN universe takes ~5 minutes keyless and seconds with a key."""

import time

MAPPING_URL = "https://api.openfigi.com/v3/mapping"


def map_isins(
    client,
    isins: list[str],
    *,
    api_key: str = "",
    on_batch=None,
    sleep=time.sleep,
) -> dict[str, list[dict]]:
    """{isin: [listing records]} for every input ISIN (missing/unmapped -> []).
    on_batch(batch_isins, response_json) fires per successful request so the
    caller can persist the verbatim response to raw.fetches."""
    headers = {"Content-Type": "application/json"}
    chunk_size = 10
    if api_key:
        headers["X-OPENFIGI-APIKEY"] = api_key
        chunk_size = 100

    out: dict[str, list[dict]] = {}
    for start in range(0, len(isins), chunk_size):
        batch = isins[start : start + chunk_size]
        jobs = [{"idType": "ID_ISIN", "idValue": isin} for isin in batch]
        while True:
            resp = client.post(MAPPING_URL, json=jobs, headers=headers)
            if resp.status_code == 429:
                # Keyless tier resets per minute; a flat wait is simpler than
                # parsing Retry-After (not always present) and costs little.
                sleep(10.0)
                continue
            resp.raise_for_status()
            break
        results = resp.json()
        if on_batch is not None:
            on_batch(batch, results)
        for isin, job in zip(batch, results):
            out[isin] = job.get("data", [])
    return out
