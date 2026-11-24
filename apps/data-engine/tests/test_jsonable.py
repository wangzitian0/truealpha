"""`to_jsonable` normalizes moomoo SDK values without importing pandas itself (C1, #1851)."""

from __future__ import annotations

import sys

import pandas as pd
import pytest
from data_engine.jsonable import is_dataframe, to_jsonable


def test_a_dataframe_becomes_its_records_and_a_non_finite_float_becomes_null() -> None:
    frame = pd.DataFrame([{"code": "US.NVDA", "close": 181.5}, {"code": "US.AMD", "close": float("nan")}])

    assert to_jsonable({"rows": frame, "ratio": float("inf")}) == {
        "rows": [{"code": "US.NVDA", "close": 181.5}, {"code": "US.AMD", "close": None}],
        "ratio": None,
    }


def test_the_dataframe_check_does_not_import_pandas(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without pandas loaded, no value can be a DataFrame, and the check must not load it."""
    monkeypatch.delitem(sys.modules, "pandas")

    assert is_dataframe({"code": "US.NVDA"}) is False
    assert "pandas" not in sys.modules
