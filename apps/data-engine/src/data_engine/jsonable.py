"""Normalize moomoo SDK return values to JSON-safe structures."""

import json
import math
import sys


def is_dataframe(obj: object) -> bool:
    """Tell a pandas DataFrame without an import of pandas. Only the moomoo SDK makes one, and it
    imports pandas first; the code server and the runs that never call moomoo do not load it (C1)."""
    pandas = sys.modules.get("pandas")
    return pandas is not None and isinstance(obj, pandas.DataFrame)


def to_jsonable(obj):
    if is_dataframe(obj):
        return json.loads(obj.to_json(orient="records"))
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, tuple | list):
        return [to_jsonable(v) for v in obj]
    # Coerce non-finite floats so Postgres jsonb inserts never see literal NaN.
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj
