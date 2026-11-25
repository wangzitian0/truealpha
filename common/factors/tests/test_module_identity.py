"""Verify structural registration invariants across FACTOR_REGISTRY (#770, #855 B3)."""

from __future__ import annotations

import factors.base.analyst_track_record  # noqa: F401
import factors.base.etf_virtual_company  # noqa: F401
import factors.base.gross_profit_per_employee  # noqa: F401
import factors.base.peg  # noqa: F401
import factors.base.price_to_sales  # noqa: F401
import factors.base.supply_chain_exposure  # noqa: F401
import factors.base.theme_purity  # noqa: F401
import factors.composite.three_tier_valuation  # noqa: F401
import pytest
from factors import FACTOR_REGISTRY

pytestmark = pytest.mark.contract


def test_factor_module_identity() -> None:
    """Every base factor sits in modules 1-6; composite sits in 7; feeders feed a composite module."""
    composite_modules = {spec.module for spec in FACTOR_REGISTRY.values() if spec.kind == "composite"}
    assert 7 in composite_modules
    for name, spec in FACTOR_REGISTRY.items():
        if spec.kind == "composite":
            assert spec.module == 7, f"{name!r} is composite but registers module={spec.module}, not 7"
        elif spec.kind == "feeder":
            assert spec.module in composite_modules, (
                f"{name!r} is a feeder registering module={spec.module}, which no composite reduces"
            )
        else:
            assert 1 <= spec.module <= 6, f"{name!r} is base but registers module={spec.module}, outside 1-6"
