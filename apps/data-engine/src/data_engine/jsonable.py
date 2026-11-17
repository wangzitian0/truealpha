"""Normalize moomoo SDK return values to JSON-safe structures."""

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
    # Coerce non-finite floats so Postgres jsonb inserts never see literal NaN.
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj
