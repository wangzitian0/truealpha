"""OpenFIGI ISIN -> listing records batch mapping client."""

import time

from data_engine.datahub.fetch_key import FetchKey
from data_engine.sources import gateway

MAPPING_URL = "https://api.openfigi.com/v3/mapping"


def map_isins(
    client,
    isins: list[str],
    *,
    api_key: str = "",
    on_batch=None,
    sleep=time.sleep,
) -> dict[str, list[dict]]:
    headers = {"Content-Type": "application/json"}
    chunk_size = 10
    if api_key:
        headers["X-OPENFIGI-APIKEY"] = api_key
        chunk_size = 100

    out: dict[str, list[dict]] = {}
    for start in range(0, len(isins), chunk_size):
        batch = isins[start : start + chunk_size]
        jobs = [{"idType": "ID_ISIN", "idValue": isin} for isin in batch]
        identity = FetchKey.for_batch("openfigi", "mapping", batch, gateway.utc_day())
        resp = gateway.http_post(
            client,
            "openfigi",
            "mapping",
            MAPPING_URL,
            identity=identity,
            caller="openfigi.map_isins",
            json=jobs,
            headers=headers,
            max_attempts=30,
            retry_statuses=(429,),
            backoff_base=10.0,
            max_backoff=10.0,
            sleep=sleep,
        )
        if resp.status_code == 429:
            raise RuntimeError(
                f"OpenFIGI kept returning 429 for 5 minutes (batch at {start}/{len(isins)}) — "
                "check service status or set OPENFIGI_API_KEY"
            )
        resp.raise_for_status()
        results = resp.json()
        if on_batch is not None:
            on_batch(batch, results)
        for isin, job in zip(batch, results):
            out[isin] = job.get("data", [])
    return out
