"""Test-only gate for integration suites that need the live runtime.

CI provisions real Postgres + MinIO precisely so the integration tests RUN
there — a silently-skipped suite reads as green while covering nothing. CI
therefore sets TRUEALPHA_REQUIRE_RUNTIME=1, turning an unreachable runtime
into a hard failure; locally (no env var) the same call skips cleanly.
"""

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

REQUIRE_RUNTIME_ENV = "TRUEALPHA_REQUIRE_RUNTIME"
TOOLS = Path(__file__).resolve().parents[4] / "tools"


def load_tool(name: str) -> ModuleType:
    """Import a `tools/<name>.py` script as a module.

    The scripts are executables, not a package, so reaching them needs
    `spec_from_file_location`. Nine test files each carried their own six-line
    copy of that bootstrap — four of them added during the session about
    deleting duplication. One copy, here, where the other test-only helper
    already lives.
    """
    path = TOOLS / f"{name}.py"
    if not path.exists():
        raise FileNotFoundError(f"no tool named {name!r} in {TOOLS}")
    spec = importlib.util.spec_from_file_location(f"truealpha_tool_{name}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    # Registered before exec so dataclasses in the tool can resolve their own
    # module during class creation — omitting this raises a confusing
    # AttributeError from dataclasses._is_type.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def skip_or_fail(reason: str) -> None:
    import pytest

    if os.environ.get(REQUIRE_RUNTIME_ENV, "").strip().lower() in {"1", "true", "yes", "on"}:
        pytest.fail(f"{REQUIRE_RUNTIME_ENV} is set but: {reason}", pytrace=False)
    pytest.skip(reason)
