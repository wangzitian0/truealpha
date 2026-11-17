"""#1238, #1239, #1272: deleted modules and files must stay deleted."""

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACTS_SRC = REPO_ROOT / "common/contracts/src/truealpha_contracts"
DATA_ENGINE_SRC = REPO_ROOT / "apps/data-engine/src/data_engine"
FACTORS_SRC = REPO_ROOT / "common/factors/src/factors"
DATA_ENGINE_SCRIPTS = REPO_ROOT / "apps/data-engine/scripts"
APP_WEB_ROOT = REPO_ROOT / "apps/app-web"
_CASES = json.loads((Path(__file__).parent / "fixtures/contract_cases.json").read_text(encoding="utf-8"))

_DELETED_MODULES = [
    *(
        (f"truealpha_contracts.{n}", CONTRACTS_SRC / f"{n}.py", CONTRACTS_SRC / n)
        for n in _CASES["deleted_contract_modules"]
    ),
    *(
        (f"data_engine.quality.{n}", DATA_ENGINE_SRC / "quality" / f"{n}.py", DATA_ENGINE_SRC / "quality" / n)
        for n in _CASES["deleted_de_quality"]
    ),
    *(
        (f"factors.{n}", FACTORS_SRC / f"{n.replace('.', '/')}.py", FACTORS_SRC / n.replace(".", "/"))
        for n in _CASES["deleted_factors"]
    ),
]


@pytest.mark.parametrize(("dotted", "file_path", "dir_path"), _DELETED_MODULES)
def test_deleted_module_stays_deleted(dotted: str, file_path: Path, dir_path: Path) -> None:
    assert importlib.util.find_spec(dotted) is None, f"{dotted} is importable again"
    assert not file_path.exists() and not dir_path.exists(), f"{dotted} path exists again"


@pytest.mark.parametrize(
    "path",
    [
        *(DATA_ENGINE_SCRIPTS / s for s in _CASES["deleted_scripts"]),
        *(APP_WEB_ROOT / f for f in _CASES["deleted_app_web"]),
    ],
)
def test_deleted_files_stay_deleted(path: Path) -> None:
    assert not path.exists(), f"{path} exists again"


def test_the_import_probe_finds_modules_that_exist() -> None:
    for mod in ("truealpha_contracts.common", "data_engine.config", "factors.registry"):
        assert importlib.util.find_spec(mod) is not None
    assert (CONTRACTS_SRC / "common.py").exists() and (DATA_ENGINE_SRC / "config.py").exists()
    assert (FACTORS_SRC / "registry.py").exists() and (DATA_ENGINE_SCRIPTS / "bootstrap_universe.py").exists()
    assert (APP_WEB_ROOT / "src/app/research/page.tsx").exists()
