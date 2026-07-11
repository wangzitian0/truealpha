"""Normalize moomoo SDK return shapes into plain json-serializable structures.

The SDK isn't uniform — endpoints return a DataFrame, a dict of DataFrames, or a
tuple of DataFrames — so ingestion normalizes whatever comes back rather than
special-casing per endpoint."""

import json

import pandas as pd


def to_jsonable(obj):
    if isinstance(obj, pd.DataFrame):
        return json.loads(obj.to_json(orient="records"))
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, tuple | list):
        return [to_jsonable(v) for v in obj]
    return obj
