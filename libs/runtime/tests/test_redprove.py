"""tools/redprove.sh must keep all its verdict paths — A4-bis E1 (#673).

The tool exists because the hand-rolled red-proof dance failed ~1 in 3 times
(no-op edits, inert-guard misreads, forgotten restores). Its self-test walks a
scratch file through every verdict: a working guard proven and restored, a
no-op edit aborted, an inert guard rejected. If the self-test rots, the tool
is the hand dance with extra steps.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_redprove_self_test_holds() -> None:
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "tools/redprove.sh"), "--self-test"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, f"self-test failed:\n{result.stdout}\n{result.stderr}"
    assert "all verdict paths hold" in result.stdout
