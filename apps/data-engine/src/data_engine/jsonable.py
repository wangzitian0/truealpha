"""Normalize moomoo SDK return shapes into plain json-serializable structures.

The SDK isn't uniform — endpoints return a DataFrame, a dict of DataFrames, or a
tuple of DataFrames — so ingestion normalizes whatever comes back rather than
special-casing per endpoint."""

import json
import math

import pandas as pd


def to_jsonable(obj):
    if isinstance(obj, pd.DataFrame):
        return json.loads(obj.to_json(orient="records"))
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, tuple | list):
        return [to_jsonable(v) for v in obj]
    # DataFrame.to_json already turns NaN/Inf into null; bare floats in plain
    # dict payloads need the same treatment, or json.dumps emits literal NaN,
    # which Postgres ::jsonb rejects at insert time.
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj
