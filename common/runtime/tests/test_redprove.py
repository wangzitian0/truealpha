# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import subprocess; from pathlib import Path; REPO_ROOT = Path(__file__).resolve().parents[3]
def test_redprove_self_test_holds() -> None: result = subprocess.run( ["bash", str(REPO_ROOT / "tools/redprove.sh"), "--self-test"], capture_output=True, text=True, check=False, timeout=60, ); assert result.returncode == 0, f"self-test failed:\n{result.stdout}\n{result.stderr}"; assert "all verdict paths hold" in result.stdout
