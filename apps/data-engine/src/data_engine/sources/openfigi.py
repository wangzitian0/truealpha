"""OpenFIGI batch mapping client."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import Any

from data_engine.datahub.fetch_key import FetchKey
from data_engine.sources import gateway

MAPPING_URL = "https://api.openfigi.com/v3/mapping"


def map_jobs(
    client: Any,
    items: Sequence[Any],
    *,
    build_job: Callable[[Any], dict[str, Any]],
    batch_token: Callable[[Any], str],
    api_key: str = "",
    caller: str = "openfigi.map_jobs",
    on_batch: Callable[[list[Any], list[dict[str, Any]], bytes, list[dict[str, Any]]], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[tuple[Any, dict[str, Any]]]:
    headers = {"Content-Type": "application/json"}
    chunk_size = 10
    if api_key:
        headers["X-OPENFIGI-APIKEY"] = api_key
        chunk_size = 100

    paired: list[tuple[Any, dict[str, Any]]] = []
    for start in range(0, len(items), chunk_size):
        batch = list(items[start : start + chunk_size])
        jobs = [build_job(item) for item in batch]
        identity = FetchKey.for_batch("openfigi", "mapping", [batch_token(item) for item in batch], gateway.utc_day())
        resp = gateway.http_post(
            client,
            "openfigi",
            "mapping",
            MAPPING_URL,
            identity=identity,
            caller=caller,
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
                f"OpenFIGI kept returning 429 for 5 minutes (batch at {start}/{len(items)}) — "
                "check service status or set OPENFIGI_API_KEY"
            )
        resp.raise_for_status()
        results = resp.json()
        if on_batch is not None:
            on_batch(batch, jobs, bytes(resp.content), results)
        paired.extend(zip(batch, results))
    return paired


def map_isins(
    client,
    isins: list[str],
    *,
    api_key: str = "",
    on_batch=None,
    sleep=time.sleep,
) -> dict[str, list[dict]]:
    def _forward_batch(
        batch: list[Any], _jobs: list[dict[str, Any]], _body: bytes, results: list[dict[str, Any]]
    ) -> None:
        if on_batch is not None:
            on_batch(batch, results)

    paired = map_jobs(
        client,
        isins,
        build_job=lambda isin: {"idType": "ID_ISIN", "idValue": isin},
        batch_token=lambda isin: isin,
        api_key=api_key,
        caller="openfigi.map_isins",
        on_batch=_forward_batch if on_batch is not None else None,
        sleep=sleep,
    )
    return {isin: job.get("data", []) for isin, job in paired}
