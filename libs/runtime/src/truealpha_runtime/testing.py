"""Test-only gate for integration suites that need the live runtime.

CI provisions real Postgres + MinIO precisely so the integration tests RUN
there — a silently-skipped suite reads as green while covering nothing. CI
therefore sets TRUEALPHA_REQUIRE_RUNTIME=1, turning an unreachable runtime
into a hard failure; locally (no env var) the same call skips cleanly.
"""

import os

REQUIRE_RUNTIME_ENV = "TRUEALPHA_REQUIRE_RUNTIME"


def skip_or_fail(reason: str) -> None:
    import pytest

    if os.environ.get(REQUIRE_RUNTIME_ENV):
        pytest.fail(f"{REQUIRE_RUNTIME_ENV} is set but: {reason}", pytrace=False)
    pytest.skip(reason)
