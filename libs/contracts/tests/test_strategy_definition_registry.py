"""#1235: the strategy definition registry, the review state it gives, and its TypeScript twin.

The registry is every packaged definition file (``strategy_definition_registry``). A recorded
run gets its review state from it: the content hash selects the entry and the version must
agree. A pair outside the registry is ``unknown``, never ``reviewed``.

The web image cannot read libs/contracts at run time, so a generated TypeScript file carries
the registry. ``test_the_typescript_registry_is_the_rendered_python_registry`` renders the
text again and fails when the checked-in file differs.
"""

from __future__ import annotations

import runpy
from importlib import resources
from pathlib import Path

import pytest
from pydantic import ValidationError
from truealpha_contracts import strategy
from truealpha_contracts.strategy import (
    HISTORICAL_V0_DEFINITION_RESOURCE,
    LIVE_STRATEGY_DEFINITION_RESOURCE,
    StrategyDefinitionIdentity,
    definition_review_state,
    load_live_strategy_definition,
    strategy_definition_registry,
)
from truealpha_contracts.strategy_run import StrategyRunDefinition

#: The two definitions as the review record states them
#: (governance/reviews/large_model_value_v0-definition-1.0.0.md). Written out here as an
#: oracle; the registry under test does not supply them.
_V0 = StrategyDefinitionIdentity("0.1.0", "e5c56da455a2845d99e67265d96b30e9b479384ef11a8a4d242494b1816fe79d", True)
_V1 = StrategyDefinitionIdentity("1.0.0", "a0c6f1f003373b5eff85872fd493de91e2fd6d9fa3d15e78e278f96b88bd8803", False)
#: A content hash that no packaged definition has.
_OUTSIDE_SHA256 = "f" * 64

_EXPORT = Path(__file__).resolve().parents[1] / "conformance" / "export_strategy_definitions.py"
_PREVIEW_FIXTURE_RESOURCE = "strategy_run_preview.v1.json"


def test_the_registry_holds_the_provisional_and_the_reviewed_definition() -> None:
    registry = strategy_definition_registry()
    assert _V0 in registry, f"definition 0.1.0 is missing from the registry: {registry}"
    assert _V1 in registry, f"definition 1.0.0 is missing from the registry: {registry}"
    live = load_live_strategy_definition()
    assert StrategyDefinitionIdentity(live.definition_version, live.content_sha256, live.provisional_semantics) in (
        registry
    ), "the definition that the deployed tick evaluates is not in the registry"


@pytest.mark.parametrize(
    ("version", "content_sha256", "expected"),
    [
        pytest.param(_V0.version, _V0.content_sha256, "provisional", id="provisional-0.1.0"),
        pytest.param(_V1.version, _V1.content_sha256, "reviewed", id="reviewed-1.0.0"),
        pytest.param(_V1.version, _OUTSIDE_SHA256, "unknown", id="hash-outside-registry"),
        pytest.param(_V0.version, _V1.content_sha256, "unknown", id="version-disagrees-with-hash"),
    ],
)
def test_the_content_hash_decides_the_review_state(version: str, content_sha256: str, expected: str) -> None:
    assert definition_review_state(version, content_sha256) == expected


def test_a_marker_cannot_claim_a_review_state_that_the_registry_does_not_give() -> None:
    with pytest.raises(ValidationError, match="review_state must be the registry state"):
        StrategyRunDefinition(version=_V1.version, content_sha256=_OUTSIDE_SHA256, review_state="reviewed")
    with pytest.raises(ValidationError, match="review_state must be the registry state"):
        StrategyRunDefinition(version=_V0.version, content_sha256=_V0.content_sha256, review_state="reviewed")
    resolved = StrategyRunDefinition.resolve(version=_V0.version, content_sha256=_V0.content_sha256)
    assert resolved.review_state == "provisional"


def _registry_of(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, files: dict[str, str]
) -> tuple[StrategyDefinitionIdentity, ...]:
    """The registry of a package directory that holds ``files`` (new name -> packaged resource)."""
    packaged = resources.files("truealpha_contracts.data")
    for name, resource in files.items():
        (tmp_path / name).write_bytes(packaged.joinpath(resource).read_bytes())
    monkeypatch.setattr(strategy.resources, "files", lambda _package: tmp_path)
    return strategy_definition_registry.__wrapped__()


def test_a_definition_file_joins_the_registry_by_its_content_not_its_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _registry_of(
        tmp_path,
        monkeypatch,
        {
            "first.json": HISTORICAL_V0_DEFINITION_RESOURCE,
            "second.json": LIVE_STRATEGY_DEFINITION_RESOURCE,
            "a_run_report.json": _PREVIEW_FIXTURE_RESOURCE,
        },
    )
    assert registry == (_V0, _V1)


def test_two_definition_files_with_one_version_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="a second packaged definition has version 1.0.0"):
        _registry_of(
            tmp_path,
            monkeypatch,
            {"first.json": LIVE_STRATEGY_DEFINITION_RESOURCE, "second.json": LIVE_STRATEGY_DEFINITION_RESOURCE},
        )


def test_the_typescript_registry_is_the_rendered_python_registry() -> None:
    export = runpy.run_path(str(_EXPORT))
    rendered: str = export["render"]()
    target: Path = export["TARGET"]
    for entry in strategy_definition_registry():
        line = (
            f'{{ version: "{entry.version}", content_sha256: "{entry.content_sha256}", '
            f"provisional_semantics: {str(entry.provisional_semantics).lower()} }},"
        )
        assert line in rendered, f"the rendered registry lost definition {entry.version}"
    assert target.read_text(encoding="utf-8") == rendered, (
        f"{target.name} differs from the Python registry. Regenerate it: "
        "uv run python libs/contracts/conformance/export_strategy_definitions.py --write"
    )
