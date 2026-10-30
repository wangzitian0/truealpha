"""#1235: the strategy definition registry, the review state it gives, and its TypeScript twins.

The registry is every packaged definition file (``strategy_definition_registry``). A recorded
run gets its review state from it: the content hash selects the entry and the version must
agree. A pair outside the registry is ``unknown``, never ``reviewed``. The owner deleted the
file of definition 0.1.0 (#1274), so a stored 0.1.0 run reads ``unknown``.

No packaged definition is provisional. ``registered_strategy_definition`` adds the synthetic
provisional definition of ``truealpha_runtime.testing`` inside one context only, so the
provisional marker stays armed for a future provisional definition.

The web image cannot read common/contracts at run time, so generated TypeScript files carry the
registry and the synthetic identity. ``test_the_typescript_files_are_the_rendered_python_sources``
renders both texts again and fails when a checked-in file differs.
"""

from __future__ import annotations

import copy
import json
import runpy
from importlib import resources
from pathlib import Path

import pytest
from pydantic import ValidationError
from truealpha_contracts.strategy import (
    LargeModelValueV0Definition,
    StrategyDefinitionIdentity,
    definition_review_state,
    load_live_strategy_corpus,
    load_live_strategy_definition,
    registered_strategy_definition,
    strategy_definition_registry,
    strategy_definitions_in,
)
from truealpha_contracts.strategy_run import StrategyRunDefinition
from truealpha_runtime.testing import synthetic_provisional_strategy_definition

#: Definition 1.0.0 as the review record states it
#: (governance/reviews/large_model_value_v0-definition-1.0.0.md). Written out here as an
#: oracle; the registry under test does not supply it.
_V1 = StrategyDefinitionIdentity("1.0.0", "a0c6f1f003373b5eff85872fd493de91e2fd6d9fa3d15e78e278f96b88bd8803", False)
#: Definition 1.1.0 (#1518, #1517): the live definition. It pins a labor-efficiency tree per
#: class and removes the confidence floor. Written out as an oracle, like `_V1`.
_V11 = StrategyDefinitionIdentity("1.1.0", "e6a49d6cee179d393bb71c89cb5f20b3a17039e4123532c8cbf92c54b30e5604", False)
#: The frozen #21 file, named on purpose: the live resource is definition 1.1.0.
_FROZEN_RESOURCE = "large_model_value_v0_definition_1_0_0.json"
#: Definition 0.1.0, whose file the owner deleted. Production and staging still hold its runs.
_DELETED_V0 = ("0.1.0", "e5c56da455a2845d99e67265d96b30e9b479384ef11a8a4d242494b1816fe79d")
#: A content hash that no definition has.
_OUTSIDE_SHA256 = "f" * 64

_EXPORT = Path(__file__).resolve().parents[1] / "conformance" / "export_strategy_definitions.py"
_PREVIEW_FIXTURE_RESOURCE = "strategy_run_preview.v1.json"


def _identity(definition: LargeModelValueV0Definition) -> StrategyDefinitionIdentity:
    return StrategyDefinitionIdentity(
        definition.definition_version, definition.content_sha256, definition.provisional_semantics
    )


def test_the_registry_holds_the_reviewed_live_definition() -> None:
    registry = strategy_definition_registry()
    assert _V1 in registry, f"definition 1.0.0 is missing from the registry: {registry}"
    assert _V11 in registry, f"definition 1.1.0 is missing from the registry: {registry}"
    assert _identity(load_live_strategy_definition()) == _V11, "the live definition is 1.1.0"
    assert _identity(load_live_strategy_definition()) in registry, (
        "the definition that the deployed tick evaluates is not in the registry"
    )


@pytest.mark.parametrize(
    ("version", "content_sha256", "expected"),
    [
        pytest.param(_V1.version, _V1.content_sha256, "reviewed", id="reviewed-1.0.0"),
        pytest.param(_V11.version, _V11.content_sha256, "reviewed", id="reviewed-1.1.0"),
        pytest.param(*_DELETED_V0, "unknown", id="deleted-0.1.0"),
        pytest.param(_V1.version, _OUTSIDE_SHA256, "unknown", id="hash-outside-registry"),
        pytest.param(_DELETED_V0[0], _V1.content_sha256, "unknown", id="version-disagrees-with-hash"),
    ],
)
def test_the_content_hash_decides_the_review_state(version: str, content_sha256: str, expected: str) -> None:
    assert definition_review_state(version, content_sha256) == expected


def test_a_marker_cannot_claim_a_review_state_that_the_registry_does_not_give() -> None:
    with pytest.raises(ValidationError, match="review_state must be the registry state"):
        StrategyRunDefinition(version=_V1.version, content_sha256=_OUTSIDE_SHA256, review_state="reviewed")
    with pytest.raises(ValidationError, match="review_state must be the registry state"):
        StrategyRunDefinition(version=_DELETED_V0[0], content_sha256=_DELETED_V0[1], review_state="provisional")
    resolved = StrategyRunDefinition.resolve(version=_DELETED_V0[0], content_sha256=_DELETED_V0[1])
    assert resolved.review_state == "unknown"


def test_the_synthetic_definition_is_provisional_through_the_real_validation() -> None:
    synthetic = synthetic_provisional_strategy_definition()
    assert synthetic.provisional_semantics is True
    assert synthetic.tier_valuation.provisional_thresholds is True
    assert _identity(synthetic) not in strategy_definition_registry(), "the synthetic definition must not be packaged"
    # The validation the builder relies on: the two provisional flags must agree.
    payload = copy.deepcopy(load_live_strategy_corpus()["strategy_definition"])
    payload["provisional_semantics"] = True
    with pytest.raises(ValidationError, match="provisional_semantics must equal the tier valuation"):
        LargeModelValueV0Definition.model_validate_json(json.dumps(payload))


def test_a_registered_definition_holds_only_inside_its_context() -> None:
    synthetic = synthetic_provisional_strategy_definition()
    version, content_sha256 = synthetic.definition_version, synthetic.content_sha256
    assert definition_review_state(version, content_sha256) == "unknown"
    with registered_strategy_definition(synthetic) as identity:
        assert identity == _identity(synthetic)
        assert identity in strategy_definition_registry()
        assert definition_review_state(version, content_sha256) == "provisional"
    assert definition_review_state(version, content_sha256) == "unknown", "the addition leaked out of its context"
    assert _identity(synthetic) not in strategy_definition_registry()


def test_registering_a_definition_that_the_registry_holds_is_refused() -> None:
    with pytest.raises(ValueError, match="the registry already holds version 1.1.0"):
        with registered_strategy_definition(load_live_strategy_definition()):
            pass


def _write_corpus(path: Path, definition: LargeModelValueV0Definition) -> None:
    path.write_text(
        json.dumps(
            {
                "strategy_definition": json.loads(definition.model_dump_json()),
                "expected": {"strategy_definition_id": definition.strategy_definition_id},
            }
        ),
        encoding="utf-8",
    )


def test_a_definition_file_joins_the_scan_by_its_content_not_its_name(tmp_path: Path) -> None:
    packaged = resources.files("truealpha_contracts.data")
    (tmp_path / "first.json").write_bytes(packaged.joinpath(_FROZEN_RESOURCE).read_bytes())
    (tmp_path / "a_run_report.json").write_bytes(packaged.joinpath(_PREVIEW_FIXTURE_RESOURCE).read_bytes())
    synthetic = synthetic_provisional_strategy_definition()
    _write_corpus(tmp_path / "second.json", synthetic)

    assert strategy_definitions_in(tmp_path) == tuple(sorted((_V1, _identity(synthetic))))


def test_two_definition_files_with_one_version_are_refused(tmp_path: Path) -> None:
    live = load_live_strategy_definition()
    _write_corpus(tmp_path / "first.json", live)
    _write_corpus(tmp_path / "second.json", live)
    with pytest.raises(ValueError, match="a second definition file has version 1.1.0"):
        strategy_definitions_in(tmp_path)


def test_the_typescript_files_are_the_rendered_python_sources() -> None:
    export = runpy.run_path(str(_EXPORT))
    rendered: dict[Path, str] = export["rendered_files"]()
    registry_text = rendered[export["TARGET"]]
    for entry in strategy_definition_registry():
        line = (
            f'{{ version: "{entry.version}", content_sha256: "{entry.content_sha256}", '
            f"provisional_semantics: {str(entry.provisional_semantics).lower()} }},"
        )
        assert line in registry_text, f"the rendered registry lost definition {entry.version}"
    synthetic = synthetic_provisional_strategy_definition()
    assert (
        f'{{ version: "{synthetic.definition_version}", content_sha256: "{synthetic.content_sha256}", '
        "provisional_semantics: true }"
    ) in rendered[export["SYNTHETIC_TARGET"]], "the rendered test helper lost the synthetic definition"
    for path, text in rendered.items():
        assert path.read_text(encoding="utf-8") == text, (
            f"{path.name} differs from its Python source. Regenerate it: "
            "uv run python common/contracts/conformance/export_strategy_definitions.py --write"
        )
