"""#770: init.md Section 7's module table is the authority for `@factor(..., module=N)`.

The two known mismatches (`price_to_sales` at `module=6`, `registered_semantic_probe` at
`module=7`) were corrected by hand in this same change, and `check_factor_contract.py`'s
I4 rule only ever asserted the coarse shape ("1-7", "module 7 is composite-only except
`price_to_sales`"). Nothing previously asserted the EXACT per-factor number against
init.md's own text, which is why the two mismatches shipped unnoticed in the first place
(init.md Section 7's now-removed note). This test parses that section instead of copying
its numbers into a second, hand-maintained mapping: if the section is renumbered or a
factor's registration drifts from it, this goes red without a matching edit here.
"""

from __future__ import annotations

import re
from pathlib import Path

import factors.base.gross_profit_per_employee  # noqa: F401
import factors.base.peg  # noqa: F401
import factors.base.price_to_sales  # noqa: F401
import factors.base.registered_semantic_probe  # noqa: F401
import factors.composite.registered_composite_probe  # noqa: F401
import factors.composite.three_tier_valuation  # noqa: F401
from factors import FACTOR_REGISTRY

REPO_ROOT = Path(__file__).resolve().parents[3]
INIT_MD = REPO_ROOT / "init.md"

#: Registered factor name -> a substring of the init.md list-item title that identifies
#: which of the seven questions it answers. Only factors that answer one of the seven
#: questions ON THEIR OWN belong here; `price_to_sales` and the two Gate-0 probes are
#: handled separately below.
_FACTOR_KEYWORDS = {
    "peg": "PEG",
    "gross_profit_per_employee": "Gross profit per employee",
    "registered_semantic_probe": "Pure-blood",
    "three_tier_valuation": "Three-tier valuation",
}


def _section_seven() -> str:
    text = INIT_MD.read_text()
    match = re.search(r"^## 7\. The Seven Analytics Modules\n(.*?)(?=\n## \d)", text, re.S | re.M)
    assert match, "init.md's '## 7. The Seven Analytics Modules' heading was not found"
    return match.group(1)


def _numbered_items(section: str) -> dict[int, str]:
    """{module number -> the list item's bold title}, parsed from init.md's own numbered
    list (`N. **Title**: ...`) rather than hardcoded, so a renumbering here is what this
    test would need to notice."""
    items = {int(number): title for number, title in re.findall(r"^(\d+)\.\s+\*\*([^*]+)\*\*", section, re.M)}
    assert items, "no numbered '**Title**' list items were parsed out of init.md Section 7"
    return items


def _price_to_sales_expected_module(section: str) -> int:
    """`price_to_sales` is documented in Section 7's prose, not its numbered list (it
    answers none of the seven questions on its own -- it feeds module 7's composite).
    Parsed from that prose rather than hardcoded, so this test tracks the same sentence a
    human reads."""
    match = re.search(r"`price_to_sales`[^.]*?registers `module=(\d+)`", section)
    assert match, "init.md Section 7 no longer documents price_to_sales's module=N registration"
    return int(match.group(1))


def test_the_seven_modules_table_parses_to_exactly_seven_items() -> None:
    items = _numbered_items(_section_seven())
    assert sorted(items) == list(range(1, 8)), f"expected modules 1-7, parsed {sorted(items)}"


def test_factor_module_identity() -> None:
    """Every registered `@factor(..., module=N)` matches init.md Section 7."""
    section = _section_seven()
    items = _numbered_items(section)

    for factor_name, keyword in _FACTOR_KEYWORDS.items():
        assert factor_name in FACTOR_REGISTRY, f"{factor_name!r} is not registered"
        matches = [number for number, title in items.items() if keyword.lower() in title.lower()]
        assert matches, f"no init.md Section 7 item title contains {keyword!r} for {factor_name!r}"
        assert len(matches) == 1, f"{keyword!r} matches more than one Section 7 item: {matches}"
        expected_module = matches[0]
        actual_module = FACTOR_REGISTRY[factor_name].module
        assert actual_module == expected_module, (
            f"{factor_name!r} registers module={actual_module}, but init.md Section 7 item "
            f"{expected_module} ({items[expected_module]!r}) is what it implements"
        )

    # `price_to_sales`: documented in prose as feeding module 7, not a numbered item of
    # its own -- its expected module comes from that sentence, not from `items`.
    expected = _price_to_sales_expected_module(section)
    actual = FACTOR_REGISTRY["price_to_sales"].module
    assert actual == expected, (
        f"price_to_sales registers module={actual}, but init.md Section 7 documents module={expected}"
    )

    # Every base factor lives in modules 1-6 except the one documented exception above;
    # module 7 is the composite (init.md: "Modules 1-6 are base factors ... Module 7 is a
    # composite factor").
    for name, spec in FACTOR_REGISTRY.items():
        if name == "price_to_sales":
            continue
        if spec.kind == "composite":
            assert spec.module == 7, f"{name!r} is composite but registers module={spec.module}, not 7"
        else:
            assert 1 <= spec.module <= 6, f"{name!r} is base but registers module={spec.module}, outside 1-6"
