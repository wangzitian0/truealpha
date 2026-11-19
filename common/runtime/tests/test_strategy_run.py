from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError
from truealpha_contracts.research import ValuationTier
from truealpha_runtime.access import AccessContext, AuthenticationMethod, PrincipalKind
from truealpha_runtime.strategy_run import (
    StrategyRunDecision,
    StrategyRunOutcome,
    StrategyRunReport,
    StrategyRunUnavailable,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "strategy_run_preview.v1.json"
_SPEC = importlib.util.spec_from_file_location(
    "strategy_run_fixture", Path(__file__).parent / "fixtures" / "strategy_run_fixture.py"
)
assert _SPEC and _SPEC.loader
_FIXTURE_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_FIXTURE_MOD)
FixtureStrategyRunRepository = _FIXTURE_MOD.FixtureStrategyRunRepository


def test_strategy_run_models_fixture_and_postgres(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)

    def _ctx(expired: bool = False) -> AccessContext:
        return AccessContext(
            context_id="ctx:t",
            principal_id="p:t",
            tenant_id="t:t",
            session_id="s:t",
            authentication_method=AuthenticationMethod.SERVICE_IDENTITY,
            principal_kind=PrincipalKind.SERVICE,
            issued_at=now - timedelta(hours=2) if expired else now,
            expires_at=now - timedelta(hours=1) if expired else now + timedelta(hours=1),
        )

    with pytest.raises(ValidationError):
        StrategyRunDecision(
            issuer_id="issuer:adm",
            cutoff_at=datetime(2026, 3, 31, 23, 59, 59),
            outcome=StrategyRunOutcome.SELECTED,
            eligible=True,
        )  # noqa: DTZ001
    assert StrategyRunDecision(
        issuer_id="issuer:adm",
        cutoff_at="2026-03-31T23:59:59Z",
        outcome=StrategyRunOutcome.SELECTED,
        eligible=True,
        confidence=Decimal("0.9"),
    ).cutoff_at == datetime(2026, 3, 31, 23, 59, 59, tzinfo=UTC)
    with pytest.raises(ValidationError):
        StrategyRunReport(
            strategy_id="large_model_value_v0", corpus_sha256="0" * 64, decisions=(), unknown_field="nope"
        )
    r0, r1 = (
        StrategyRunReport(strategy_id="large_model_value_v0", corpus_sha256="0" * 64, decisions=()),
        StrategyRunReport(
            strategy_id="large_model_value_v0",
            corpus_sha256="0" * 64,
            decisions=(),
            strategy_run_id="run:123",
            governed=True,
        ),
    )
    assert r0.strategy_run_id is None and not r0.governed and r1.strategy_run_id == "run:123" and r1.governed
    with pytest.raises(ValidationError):
        StrategyRunDecision(
            issuer_id="issuer:adm",
            cutoff_at="2026-03-31T23:59:59Z",
            outcome=StrategyRunOutcome.EXCLUDED,
            eligible=False,
            confidence=Decimal("1.5"),
        )
    payload = json.loads(FIXTURE_PATH.read_bytes())
    assert (
        payload["strategy_id"] == "large_model_value_v0"
        and payload["source"] == "mart"
        and payload["golden_mismatches"] == []
        and "generated_at" not in payload
        and len(payload["decisions"]) == 10
    )
    repo = FixtureStrategyRunRepository()
    rep = repo.get_latest(strategy_id="large_model_value_v0", context=_ctx())
    assert (
        isinstance(rep, StrategyRunReport)
        and rep.corpus_sha256 == payload["corpus_sha256"]
        and len(rep.decisions) == len(payload["decisions"])
        and rep.strategy_run_id == "test_fixture"
        and not rep.governed
    )
    sel = next(d for d in rep.decisions if d.issuer_id == "issuer:adm" and d.cutoff_at.month == 3)
    exp = next(
        d for d in payload["decisions"] if d["issuer_id"] == "issuer:adm" and d["cutoff_at"].startswith("2026-03")
    )
    assert (
        sel.outcome.value,
        sel.tier,
        str(sel.valuation_gap),
        str(sel.confidence),
        sel.rank,
        str(sel.target_weight),
    ) == (
        exp["outcome"],
        ValuationTier(exp["tier"]),
        exp["valuation_gap"],
        exp["confidence"],
        exp["rank"],
        exp["target_weight"],
    )
    excl = next(d for d in rep.decisions if d.exclusion_reason == "below_confidence_floor")
    assert not excl.eligible and excl.confidence is not None
    assert repo.get_latest(strategy_id="does_not_exist", context=_ctx()).reason == "unknown_strategy_id"
    assert isinstance(
        repo.get_latest(strategy_id="large_model_value_v0", context=_ctx(expired=True)), StrategyRunReport
    )
    monkeypatch.setattr(_FIXTURE_MOD, "FIXTURE_PATH", Path("/nonexistent/strategy_run_preview.v1.json"))
    assert repo.get_latest(strategy_id="large_model_value_v0", context=_ctx()).reason == "no_runs_recorded"
    bad = tmp_path / "bad.json"
    bad.write_bytes(b"{not valid json")
    monkeypatch.setattr(_FIXTURE_MOD, "FIXTURE_PATH", bad)
    assert repo.get_latest(strategy_id="large_model_value_v0", context=_ctx()).reason == "schema_mismatch"

    from truealpha_runtime.strategy_run_postgres import PostgresStrategyRunRepository

    _Cur = type(
        "_C",
        (),
        {
            "__enter__": lambda s: s,
            "__exit__": lambda *a: None,
            "execute": lambda *a: None,
            "fetchone": lambda s: {"strategy_run_id": "t", "corpus_sha256": "0" * 64},
            "fetchall": lambda s: [],
        },
    )
    _Conn = type("_K", (), {"__enter__": lambda s: s, "__exit__": lambda *a: None, "cursor": lambda s, **k: _Cur()})
    monkeypatch.setattr("psycopg.connect", lambda *a, **k: _Conn())
    res = PostgresStrategyRunRepository(database_url="postgresql://fake").get_latest(
        strategy_id="large_model_value_v0", context=_ctx()
    )
    assert isinstance(res, StrategyRunUnavailable) and res.reason == "empty_decisions"


def test_latest_run_params_and_mart_readonly_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    import inspect

    from truealpha_contracts.universes import id_prefix_like
    from truealpha_runtime import strategy_run_postgres

    assert "recorded_at" not in inspect.getsource(strategy_run_postgres)
    prefixes_all, key_all = strategy_run_postgres.latest_run_params("large_model_value_v0")
    assert key_all == "large_model_value_v0" and id_prefix_like("topt") in prefixes_all
    assert strategy_run_postgres.latest_run_params("large_model_value_v0", universe="qqq") == (
        [id_prefix_like("qqq")],
        "large_model_value_v0",
    )

    executed: list[tuple[str, object]] = []
    connect_kwargs: list[dict[str, object]] = []

    class _Cur:
        def __enter__(self) -> _Cur:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

        def execute(self, sql: str, params: object = None) -> None:
            executed.append((sql, params))

        def fetchone(self) -> None:
            return None

        def fetchall(self) -> list[object]:
            return []

    class _Conn:
        def __enter__(self) -> _Conn:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

        def cursor(self, **_k: object) -> _Cur:
            return _Cur()

    def _fake_connect(_dsn: str, **kwargs: object) -> _Conn:
        connect_kwargs.append(kwargs)
        return _Conn()

    monkeypatch.setattr("psycopg.connect", _fake_connect)
    now = datetime.now(UTC)
    ctx = AccessContext(
        context_id="ctx:t",
        principal_id="p:t",
        tenant_id="t:t",
        session_id="s:t",
        authentication_method=AuthenticationMethod.SERVICE_IDENTITY,
        principal_kind=PrincipalKind.SERVICE,
        issued_at=now,
        expires_at=now + timedelta(hours=1),
    )
    res = strategy_run_postgres.PostgresStrategyRunRepository(database_url="postgresql://fake").get_latest(
        strategy_id="large_model_value_v0", context=ctx, universe="qqq"
    )
    assert isinstance(res, StrategyRunUnavailable) and res.reason == "no_runs_recorded"
    assert "autocommit" not in connect_kwargs[0]
    assert executed[0][0] == "set local role mart_readonly"
    assert executed[1][0] == "select set_config('statement_timeout', '5s', true)"
    assert executed[2][1] == ([id_prefix_like("qqq")], "large_model_value_v0")
    assert executed[3][0] == "reset role"
