"""The reachability ratchet covers `libs/factors`, and per tree (#791).

`tools/reachability_ratchet.py` guarded `apps/data-engine/src` alone and reported 0
unreachable lines while `libs/factors` — where the factors are — was never walked. This
test is the standing check for the extension, and for the property that makes it worth
having: the count is judged PER TREE, so a deletion in the data engine cannot pay for an
accretion in the factor library.

Static and stdlib-only, like the tool: it drives the tool's own functions over the real
repository rather than re-implementing the walk, so a change to the walk that breaks the
guarantee fails here instead of being described here.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
TOOL = ROOT / "tools" / "reachability_ratchet.py"


def _tool():
    spec = importlib.util.spec_from_file_location("reachability_ratchet", TOOL)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["reachability_ratchet"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def ratchet():
    return _tool()


def test_both_source_trees_are_walked(ratchet) -> None:
    """The defect this extension fixes: only one tree had an objector."""
    assert set(ratchet.TREES) == {"data_engine", "factors"}
    for tree, src in ratchet.TREES.items():
        assert src.is_dir(), f"{tree} source root {src} does not exist"
    modules = ratchet._modules()
    trees = {ratchet._tree_of(name) for name in modules}
    assert trees == {"data_engine", "factors"}, "the census must contain modules from both trees"


def test_factor_modules_are_reachable_through_the_deployed_root(ratchet) -> None:
    """A factor the tick actually computes must not be counted dead — otherwise the
    number is noise and the next reader learns to ignore it."""
    _lines, dead = ratchet.unreachable()
    for live in (
        "factors.base.gross_profit_per_employee",
        "factors.base.etf_virtual_company",
        "factors.composite.three_tier_valuation",
        "factors.production_topt.core",
        "factors.shared.extraction",
    ):
        assert live not in dead, f"{live} is computed by the deployed tick but counted unreachable"


def test_the_count_is_per_tree_not_a_single_total(ratchet) -> None:
    """A single total lets one tree's deletion mask another tree's accretion — the one
    thing the ratchet exists to catch."""
    lines, _dead = ratchet.unreachable()
    assert set(lines) == {"data_engine", "factors"}
    baseline = ratchet._read_baseline()
    assert set(baseline) == {"data_engine", "factors"}
    assert all(isinstance(value, int) for value in lines.values())


def test_the_committed_baseline_matches_the_tree(ratchet) -> None:
    """The baseline is a measurement of THIS commit; drift means someone changed the
    reachable set without tightening or explaining it."""
    lines, _dead = ratchet.unreachable()
    baseline = ratchet._read_baseline()
    for tree in ratchet.TREES:
        assert lines[tree] <= baseline[tree], (
            f"{tree}: {lines[tree]} unreachable lines exceeds baseline {baseline[tree]} — "
            "wire the module into the deployed path or remove it"
        )


def test_a_standards_adapter_string_is_a_wiring(ratchet) -> None:
    """`STANDARDS` declares each standard's adapter as "module:function" and
    `backfill._adapter` resolves it at run time (#800). That string IS how the deployed loop
    reaches the module, so the walk must follow it — otherwise landing a second standard's
    adapter turns the ratchet red for a module production genuinely calls, and the only way
    to green is to raise the baseline, which is the ratchet failing at its job.

    Asserted through the real registry rather than a fixture: if a standard stops declaring
    an adapter, or the walk stops reading it, this goes red.
    """
    from truealpha_contracts.standards import STANDARDS

    _lines, dead = ratchet.unreachable()
    declared = {standard.adapter.partition(":")[0] for standard in STANDARDS.values()}
    assert declared, "no standard declares an adapter; this test's premise is gone"
    for module in declared:
        assert module in ratchet._modules(), f"{module} is declared as an adapter but does not exist"
        assert module not in dead, (
            f"{module} is the adapter a standard declares, so the deployed loop reaches it — "
            "the walk must follow the registry string"
        )


def test_an_unimported_factor_module_is_caught(ratchet) -> None:
    """Red-proof: a new factor module nobody imports must raise the factor tree's count.

    The probe is written into the real tree and removed in a `finally`, because the walk
    reads the tree from disk — a temp copy would exercise a path the tool never takes. The
    last assertion is what makes that safe to say: the census must return to its previous
    value once the probe is gone.
    """
    before, _ = ratchet.unreachable()

    # Derived from a real module's own path rather than spelled out: the package dir
    # under the source root is `factors/`, and a hardcoded guess silently writes the
    # probe somewhere the walk never looks (which is how this test first passed nothing).
    anchor = ratchet._modules()["factors.base.gross_profit_per_employee"]
    orphan = anchor.with_name("_ratchet_probe_orphan.py")
    orphan.write_text('"""Imported by nothing. The ratchet must say so."""\n\nVALUE = 1\n')
    try:
        after, dead = ratchet.unreachable()
    finally:
        orphan.unlink()

    assert after["factors"] > before["factors"], "an unimported factor module must raise the count"
    assert "factors.base._ratchet_probe_orphan" in dead
    # And the other tree must be untouched: the failure has to name the tree that moved.
    assert after["data_engine"] == before["data_engine"]

    restored, _ = ratchet.unreachable()
    assert restored == before, "the probe must leave no trace"
