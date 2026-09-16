"""Tests for tools/datahub_freshness.py — the page the governed pointer never had.

2026-08-15/17: a Twelve Data per-minute collision froze the governed head for three
days in both environments; the #536 gate was right to withhold, and nothing was red.
The check reads `/api/health`'s `governed_pointers` and bounds each universe's age.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from truealpha_runtime.testing import load_tool

_module = load_tool("datahub_freshness")
check = _module.check_pointer_freshness
MAX_AGE_HOURS = _module.MAX_AGE_HOURS

URL = "https://truealpha-staging.truealpha.club/api/health"
NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


def _health(pointers: object, *, status: int = 200) -> object:
    def http_get(url: str) -> tuple[int, str]:
        return status, json.dumps({"status": "ok", "git_sha": "v0.0.65", "governed_pointers": pointers})

    return http_get


def _head(universe: str, hours_ago: float) -> dict[str, object]:
    advanced = NOW - timedelta(hours=hours_ago)
    return {"universe_id": universe, "advanced_at": advanced.isoformat(), "age_hours": hours_ago}


def test_pointers_advancing_within_the_bound_pass(capsys: pytest.CaptureFixture[str]) -> None:
    code = check(URL, environment="staging", http_get=_health([_head("universe:topt", 14.0)]), now=NOW)
    assert code == 0
    assert "advancing" in capsys.readouterr().out


def test_a_frozen_universe_fails_and_is_named(capsys: pytest.CaptureFixture[str]) -> None:
    """The August shape: one universe stops advancing while the ticks look green."""
    heads = [_head("universe:topt", MAX_AGE_HOURS + 6), _head("universe:qqq", 10.0)]
    code = check(URL, environment="production", http_get=_health(heads), now=NOW)
    assert code == 1
    err = capsys.readouterr().err
    assert "universe:topt" in err and "frozen" in err and "universe:qqq" not in err


def test_the_age_is_measured_against_the_checkers_clock_not_the_services() -> None:
    """A service reporting age_hours=1 for a timestamp four days old is wrong, not fresh."""
    stale_but_lying = {**_head("universe:topt", 96.0), "age_hours": 1.0}
    assert check(URL, http_get=_health([stale_but_lying]), now=NOW) == 1


def test_no_pointer_at_all_fails(capsys: pytest.CaptureFixture[str]) -> None:
    assert check(URL, environment="staging", http_get=_health([]), now=NOW) == 1
    assert "no governed pointer" in capsys.readouterr().err


def test_an_unreadable_report_fails_never_passes_by_omission(capsys: pytest.CaptureFixture[str]) -> None:
    assert check(URL, http_get=_health("unknown"), now=NOW) == 1
    assert "unknown" in capsys.readouterr().err


def test_a_release_before_the_report_is_not_a_failure(capsys: pytest.CaptureFixture[str]) -> None:
    """Production may serve an older build for days (deploy-freshness bounds that lag);
    a missing key is an older release, not a frozen pointer."""

    def http_get(url: str) -> tuple[int, str]:
        return 200, json.dumps({"status": "ok", "git_sha": "v0.0.56"})

    assert check(URL, environment="production", http_get=http_get, now=NOW) == 0
    assert "predates" in capsys.readouterr().out


def test_unreachable_or_non_json_fails() -> None:
    assert check(URL, http_get=_health([], status=503), now=NOW) == 1

    def broken(url: str) -> tuple[int, str]:
        return 200, "<html>gateway</html>"

    assert check(URL, http_get=broken, now=NOW) == 1


def test_a_malformed_entry_fails_loudly() -> None:
    assert check(URL, http_get=_health([{"universe_id": "universe:topt"}]), now=NOW) == 1
