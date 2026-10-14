"""#1238, #1239, #1272: modules deleted across truealpha packages must not come back.

Each module or script below had no importer outside its own tests or was dead code.
Nothing deployed used it, so the code only added review and merge cost.
This test fails when any listed module name is importable again or when any deleted file exists again.
Positive controls for each package ensure that the import probe and file existence checks are functioning.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACTS_SRC = REPO_ROOT / "libs" / "contracts" / "src" / "truealpha_contracts"
DATA_ENGINE_SRC = REPO_ROOT / "apps" / "data-engine" / "src" / "data_engine"
FACTORS_SRC = REPO_ROOT / "libs" / "factors" / "src" / "factors"
DATA_ENGINE_SCRIPTS = REPO_ROOT / "apps" / "data-engine" / "scripts"
APP_WEB_ROOT = REPO_ROOT / "apps" / "app-web"

DELETED_CONTRACT_MODULES = (
    "gates",
    "policy_bundle",
    "market",
    "documents",
    "conversations",
    "capture_contracts",
    "readiness",
)

DELETED_DATA_ENGINE_QUALITY_MODULES = (
    "confidence",
    "strategy_samples",
)

DELETED_FACTORS_MODULES = (
    "confidence",
    "composite.registered_composite_probe",
)

DELETED_SCRIPTS = (
    "datahub_quality_report.py",
    "refresh_etf_universe.py",
    "run_production_topt_capture.py",
    "materialize_production_topt_core.py",
    "query_production_topt_datahub.py",
)

DELETED_APP_WEB_FILES = (
    "src/app/research/conversations/page.tsx",
    "src/app/research/conversations/[id]/page.tsx",
    "src/server/conversations-actions.ts",
    "src/server/conversations-answer.ts",
    "src/server/conversations.ts",
    "tests/conversations-answer.test.ts",
    "tests/conversations-validation.test.ts",
)


@pytest.mark.parametrize("name", DELETED_CONTRACT_MODULES)
def test_deleted_contract_module_is_not_importable(name: str) -> None:
    spec = importlib.util.find_spec(f"truealpha_contracts.{name}")
    assert spec is None, f"truealpha_contracts.{name} was deleted in #1238 and is importable again: {spec.origin}"


@pytest.mark.parametrize("name", DELETED_CONTRACT_MODULES)
def test_deleted_contract_module_has_no_source_file(name: str) -> None:
    assert not (CONTRACTS_SRC / f"{name}.py").exists(), f"{name}.py was deleted in #1238 and exists again"
    assert not (CONTRACTS_SRC / name).exists(), f"package {name}/ was deleted in #1238 and exists again"


@pytest.mark.parametrize("name", DELETED_DATA_ENGINE_QUALITY_MODULES)
def test_deleted_data_engine_quality_module_is_not_importable(name: str) -> None:
    spec = importlib.util.find_spec(f"data_engine.quality.{name}")
    assert spec is None, f"data_engine.quality.{name} was deleted in #1239 and is importable again: {spec.origin}"


@pytest.mark.parametrize("name", DELETED_DATA_ENGINE_QUALITY_MODULES)
def test_deleted_data_engine_quality_module_has_no_source_file(name: str) -> None:
    quality_dir = DATA_ENGINE_SRC / "quality"
    assert not (quality_dir / f"{name}.py").exists(), f"quality/{name}.py was deleted in #1239 and exists again"
    assert not (quality_dir / name).exists(), f"quality/{name}/ was deleted in #1239 and exists again"


@pytest.mark.parametrize("name", DELETED_FACTORS_MODULES)
def test_deleted_factors_module_is_not_importable(name: str) -> None:
    spec = importlib.util.find_spec(f"factors.{name}")
    assert spec is None, f"factors.{name} was deleted in #1239 and is importable again: {spec.origin}"


@pytest.mark.parametrize("name", DELETED_FACTORS_MODULES)
def test_deleted_factors_module_has_no_source_file(name: str) -> None:
    rel_path = name.replace(".", "/")
    assert not (FACTORS_SRC / f"{rel_path}.py").exists(), f"factors/{rel_path}.py was deleted in #1239 and exists again"
    assert not (FACTORS_SRC / rel_path).exists(), f"factors/{rel_path}/ was deleted in #1239 and exists again"


@pytest.mark.parametrize("script_name", DELETED_SCRIPTS)
def test_deleted_scripts_have_no_source_file(script_name: str) -> None:
    assert not (DATA_ENGINE_SCRIPTS / script_name).exists(), f"scripts/{script_name} was deleted and exists again"


@pytest.mark.parametrize("rel_path", DELETED_APP_WEB_FILES)
def test_deleted_app_web_files_have_no_source_file(rel_path: str) -> None:
    assert not (APP_WEB_ROOT / rel_path).exists(), f"apps/app-web/{rel_path} was deleted in #1272 and exists again"


def test_the_import_probe_finds_modules_that_exist() -> None:
    """Positive controls. Probes that find nothing for every name would pass the tests above for any input."""
    # Contracts control
    assert importlib.util.find_spec("truealpha_contracts.access") is not None
    assert (CONTRACTS_SRC / "access.py").exists()

    # Data engine control
    assert importlib.util.find_spec("data_engine.config") is not None
    assert (DATA_ENGINE_SRC / "config.py").exists()

    # Factors control
    assert importlib.util.find_spec("factors.registry") is not None
    assert (FACTORS_SRC / "registry.py").exists()

    # Scripts control
    assert (DATA_ENGINE_SCRIPTS / "bootstrap_universe.py").exists()

    # App-web control
    assert (APP_WEB_ROOT / "src" / "app" / "research" / "page.tsx").exists()
