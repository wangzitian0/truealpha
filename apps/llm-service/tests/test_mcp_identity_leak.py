"""#953: no MCP tool response carries a bare-UUID-shaped identity field.

This is the third instance of one defect, not three defects. #928 made every mart entity
coordinate an opaque UUID; each consumer that echoed one straight out of a payload then
served callers an id nothing outside this database can resolve:

  1. `topt_gppe.listing_id`  -- fixed per-tool by #954/#967.
  2. `strategy_run.issuer_id` -- `issuer:lei:29DX7H14B9S6O3FD6V18` on staging v0.0.90,
     `02587046-dc99-5a44-b811-e2d086a58ccb` from v0.0.91 on.
  3. the consumer inventory in docs/entity-identity.md §6, which listed neither.

A per-tool test invites a fourth, so this test is written against the CLASS. It takes the
registered tool list from the deployed server (`build_mcp_server`, the same function
`mcp_server.mcp` is built with) and refuses to run unless every registered tool has a call
recipe here: registering a tool without covering it fails this test, rather than passing
silently until someone remembers.

It runs the REAL repositories -- no injected fakes -- against a real Postgres seeded with a
decision keyed by a real minted entity UUID, so the check fails where production calls. All
seeding happens inside one transaction that is rolled back; `psycopg.connect` is redirected
onto it, the same seam test_strategy_run_parity_conformance.py uses.

Red-proven against the unfixed read (`select d.issuer_id`):

    AssertionError: MCP responses carry bare-UUID identity fields:
      strategy_run: result.decisions[0].issuer_id = 4f0a...-...
      research_report: subjects[0].subject_id = 4f0a...-...
"""

from __future__ import annotations

import json
import os
import re
import secrets
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from llm_service.mcp_server import build_mcp_server
from psycopg.rows import dict_row

pytestmark = [pytest.mark.gate, pytest.mark.deps]

# The canonical 8-4-4-4-12 form. `staging.entities.entity_id` is a uuidv5, so this is
# exactly what an untranslated mart entity coordinate looks like on the wire.
_BARE_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)

# A field that NAMES an entity. Structural, not a list of known leaks: the point is to
# cover fields nobody has thought of yet.
_IDENTITY_FIELD = re.compile(r"(^|_)ids?$")

# Identity fields that may legitimately hold a UUID, by EXACT name, each with its reason.
# Exempt a name here rather than weakening _BARE_UUID or _IDENTITY_FIELD -- a looser
# pattern stops covering the next field as well as this one.
#
# Empty today: every identity a tool returns is either a prefixed content address
# (`report:<sha>`, `strategy-run:<sha>`, `card:<sha>`) or an entity coordinate, and an
# entity coordinate must be resolvable. A future field that is genuinely an opaque handle
# -- a pagination cursor, a trace id minted per call -- belongs here with that sentence.
_EXEMPT_IDENTITY_FIELDS: dict[str, str] = {}

_CUTOFF = "2026-03-31T23:59:59Z"

# Every registered tool, with arguments that reach its read path. `test_every_registered_
# tool_is_covered` fails if this does not match what the server registers, so a new tool
# cannot be added without deciding how it gets called here.
_TOOL_CALLS: dict[str, dict[str, Any]] = {
    "admin_ops_snapshot": {},
    "admin_datahub_facts": {},
    "panel_universes": {},
    "panel_coverage": {"request": {"node_key": "gppe_uniform_charge_v0"}},
    "panel_macro": {},
    "strategy_rankings": {"request": {"strategy_id": "large_model_value_v0"}},
    "strategy_backtests": {"request": {"strategy_key": "qqq_topk_rank", "limit": 5}},
    "strategy_backtest_detail": {"request": {"run_id": "backtest-run:unused"}},
    "strategy_backtest_trigger": {"request": {"executed_at": _CUTOFF}},
    "entities_holdings": {"request": {"fund_id": "etf:series:S000101292"}},
    "entities_themes": {"request": {"theme_id": "ai-infrastructure", "limit": 10}},
    "entities_trace": {"request": {"issuer_id": "issuer:ranking-scope-unused"}},
    "entities_supply_chain": {"request": {"issuer_id": "issuer:ranking-scope-unused"}},
    "entities_analyst_ratings": {"request": {"issuer_id": "issuer:ranking-scope-unused"}},
    "entities_detail": {"request": {"issuer_id": "issuer:ranking-scope-unused"}},
    "library_report": {
        "request": {
            "report_kind": "theme_ranking",
            "target_entity_ids": ["issuer:ranking-scope-unused"],
            "cutoff_at": _CUTOFF,
        }
    },
    "library_card_export": {
        "request": {
            "report_kind": "theme_ranking",
            "target_entity_ids": ["issuer:ranking-scope-unused"],
            "cutoff_at": _CUTOFF,
            "card_kind": "ranking",
            "format": "json",
        }
    },
    "library_documents_list": {},
    "library_document_create": {
        "request": {
            "source_artifact_id": "note:identity-leak-probe",
            "content": "probe-body",
        }
    },
    "library_document_detail": {"request": {"document_id": "document:unused"}},
}


def _database_url() -> str | None:
    return os.environ.get("DATABASE_URL")


class _BorrowedConnection:
    """Redirects each repository's per-call ``psycopg.connect`` onto the test's own
    transaction; close/commit are withheld so the rollback owns the data. Carries both
    access shapes the repositories use: ``cursor(...)`` and ``execute(...)``."""

    def __init__(self, real: psycopg.Connection, row_factory: Any = None) -> None:
        self._real = real
        self._row_factory = row_factory

    def __enter__(self) -> _BorrowedConnection:
        return self

    def __exit__(self, *_exc: object) -> None:
        self._real.execute("reset role")
        return None

    def cursor(self, **kwargs: Any) -> Any:
        if self._row_factory is not None and "row_factory" not in kwargs:
            kwargs["row_factory"] = self._row_factory
        return self._real.cursor(**kwargs)

    @property
    def connection(self) -> psycopg.Connection:
        return self._real

    def rollback(self) -> None:
        return None

    def execute(self, query: Any, params: Any = None, **kwargs: Any) -> Any:
        # topt_read connects with row_factory=dict_row and reads rows by key.
        return self._real.cursor(row_factory=dict_row).execute(query, params, **kwargs)


def _walk(node: Any, path: str) -> Iterator[tuple[str, str, str]]:
    """Yields (path, field_name, value) for every scalar under an identity-shaped key."""
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            if _IDENTITY_FIELD.search(str(key)):
                if isinstance(value, str):
                    yield child, str(key), value
                elif isinstance(value, list):
                    for index, item in enumerate(value):
                        if isinstance(item, str):
                            yield f"{child}[{index}]", str(key), item
            yield from _walk(value, child)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from _walk(item, f"{path}[{index}]")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def seeded_entity(monkeypatch: pytest.MonkeyPatch):
    """One issuer, minted for real, recorded in a strategy decision by its UUID.

    Real `staging.entity_mint` and real aliases: `staging.validate_entity` rejects a UUID
    that is not the uuidv5 its birth alias derives, so a hand-written pair could not stand
    in for one. That is the whole point -- the fixture has to be the same coordinate
    #928's writer produces.
    """
    from truealpha_runtime.testing import skip_or_fail

    url = _database_url()
    if not url:
        skip_or_fail("DATABASE_URL must be explicitly provided for MCP identity tests (no default to port 5432)")
        return
    try:
        connection = psycopg.connect(url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; ci-python runs this against a real database")
    try:
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        lei = "".join(secrets.choice(alphabet) for _ in range(18)) + f"{secrets.randbelow(100):02d}"
        legacy_id = f"issuer:lei:{lei}"
        entity_uuid = str(
            connection.execute(
                "select staging.entity_mint('issuer', 'lei', %s, 'test-mcp-identity-leak')", (lei,)
            ).fetchone()[0]
        )
        connection.execute(
            """
            insert into staging.entity_aliases
              (entity_id, scheme, value, valid_from, transaction_time, source, raw_ref,
               method, confidence, mapping_version)
            values (%s, 'lei', %s, date '2026-01-01', timestamptz '2026-01-01',
                    'test-mcp-identity-leak', 'test-ref', 'asserted', 1.0, 'v1'),
                   (%s, 'legacy-id', %s, date '2026-01-01', timestamptz '2026-01-01',
                    'test-mcp-identity-leak', 'test-ref', 'asserted', 1.0, 'v1')
            """,
            (entity_uuid, lei, entity_uuid, legacy_id),
        )
        from truealpha_runtime.mart_refresh import (
            MART_ENTITY_IDENTITY,
            refresh_mart_materialized_views,
        )

        refresh_mart_materialized_views(connection, (MART_ENTITY_IDENTITY,))
        run_id = "strategy-run:" + secrets.token_hex(32)
        connection.execute(
            """
            insert into mart.strategy_runs
              (strategy_run_id, content_sha256, strategy_key, strategy_version,
               definition_content_sha256, corpus_sha256, claim_ceiling, executed_at)
            values (%s, %s, 'large_model_value_v0', 'v0', %s, %s, 'preview', timestamptz '2027-07-01')
            """,
            (run_id, secrets.token_hex(32), secrets.token_hex(32), secrets.token_hex(32)),
        )
        connection.execute(
            """
            insert into mart.strategy_decisions
              (strategy_decision_id, content_sha256, strategy_run_id, issuer_id, cutoff_at,
               capital_adjusted_labor_efficiency, tier, current_price_to_sales,
               target_price_to_sales, valuation_gap, eligible, outcome, rank, target_weight)
            values (%s, %s, %s, %s, %s, '12.5', 'tech', '20', '18.75', '0.5', true, 'selected', 1, '1.0')
            """,
            (
                "strategy-decision:" + secrets.token_hex(32),
                secrets.token_hex(32),
                run_id,
                entity_uuid,
                _CUTOFF,
            ),
        )
        tok = secrets.token_hex(32)
        policy_id = f"mcp-leak-policy:{tok[:12]}"
        sp_id = f"schedule-policy:{tok}"
        scope_id = f"capture-scope:{tok}"
        env_row = connection.execute("select environment from mart.environment_identity").fetchone()
        env_name = str(env_row[0]) if env_row else "production"
        cutoff = "2026-06-30T00:00:00Z"
        camp_row = connection.execute(
            "with p as (select jsonb_build_object('campaign_policy_id', %s::text, 'environment', %s::text, 'cutoff', %s::text, "
            "'universe_refs', jsonb_build_array(jsonb_build_object('content_sha256', repeat('d', 64), 'universe_id', 'universe:topt-us-2026-03-31', 'universe_version', '2026-03-31'))) as payload) "
            "select 'capture-campaign:' || raw.canonical_sha256(jsonb_build_object('kind', 'capture-campaign', 'identity', payload)), "
            "raw.canonical_sha256(payload), (payload->'universe_refs')::text from p",
            (policy_id, env_name, cutoff),
        ).fetchone()
        assert camp_row is not None
        camp_id, camp_sha, urefs_json = str(camp_row[0]), str(camp_row[1]), str(camp_row[2])
        connection.execute(
            "insert into raw.capture_campaigns (campaign_id, content_sha256, policy_id, environment, cutoff, cutoff_canonical, universe_refs) "
            "values (%s, %s, %s, %s, %s::timestamptz, %s, %s::jsonb)",
            (camp_id, camp_sha, policy_id, env_name, cutoff, cutoff, urefs_json),
        )
        connection.execute(
            "insert into raw.capture_schedule_policies (schedule_policy_id, content_sha256, policy_version, demanded_cadence, provider_availability_cadence, freshness_max_age, retry_policy, payload) "
            "values (%s, %s, %s, interval '1 day', 'manual-only:v1', interval '30 days', '{}'::jsonb, '{}'::jsonb)",
            (sp_id, tok, policy_id),
        )
        cap_row = connection.execute(
            "with p as (select jsonb_build_object('campaign_id', %s::text, 'run_sequence', 1, 'schedule_policy_id', %s::text, 'capture_scope_id', %s::text) as payload) "
            "select 'capture-run:' || raw.canonical_sha256(jsonb_build_object('kind', 'capture-run', 'identity', payload)), "
            "raw.canonical_sha256(payload) from p",
            (camp_id, sp_id, scope_id),
        ).fetchone()
        assert cap_row is not None
        cap_id, cap_sha = str(cap_row[0]), str(cap_row[1])
        connection.execute(
            "insert into raw.capture_runs (run_id, campaign_id, run_sequence, schedule_policy_id, capture_scope_id, content_sha256) "
            "values (%s, %s, 1, %s, %s, %s)",
            (cap_id, camp_id, sp_id, scope_id, cap_sha),
        )
        connection.execute(
            "insert into staging.evidence_nodes (node_id, kind, content_sha256, valid_from, transaction_time, recorded_at) "
            "values (%s, 'capture_run', %s, '2026-03-31', now(), now())",
            (cap_id, cap_id.split(":", 1)[1]),
        )
        connection.execute(
            "insert into mart.strategy_run_capture_bindings (capture_run_id, strategy_run_id, bound_by) "
            "values (%s, %s, 'tick')",
            (cap_id, run_id),
        )
        connection.execute(
            "insert into mart.current_pointer (pointer_id, content_sha256, environment, universe_id, universe_version, "
            "factor_id, target_run_id, sequence, previous_run_id, advanced_at) "
            "values (%s, %s, %s, 'universe:topt-us-2026-03-31', %s, 'gross_profit_per_employee', %s, 0, null, now())",
            (f"current-pointer:{tok}", tok, env_name, f"v1-{tok[:8]}", cap_id),
        )
        from llm_service.services import library_service

        connection.execute("insert into app.tenants (tenant_id) values ('tenant:truealpha') on conflict do nothing")
        connection.execute(
            "insert into app.principals (principal_id, tenant_id, principal_kind) values "
            "('principal:owner', 'tenant:truealpha', 'administrator') on conflict do nothing"
        )
        monkeypatch.setattr(
            library_service,
            "_put_s3_bytes",
            lambda *_a, **_k: ("documents/principal:owner/aa/" + "a" * 64, "a" * 64, 10),
        )
        monkeypatch.setattr(
            psycopg,
            "connect",
            lambda *args, **kwargs: _BorrowedConnection(connection, kwargs.get("row_factory")),
        )
        yield {"entity_uuid": entity_uuid, "legacy_id": legacy_id}
    finally:
        connection.rollback()
        connection.close()


@pytest.mark.anyio
async def test_every_registered_tool_is_covered() -> None:
    """The enumeration IS the coverage. Registering a fifth tool without giving it a call
    recipe fails here, which is the difference between a guard over the class and four
    guards over four instances."""
    server = build_mcp_server()
    registered = {tool.name for tool in await server.list_tools()}
    assert registered == set(_TOOL_CALLS), (
        f"registered tools {sorted(registered)} do not match the covered tools "
        f"{sorted(_TOOL_CALLS)}. A new MCP tool must declare how this guard calls it, "
        f"or its responses go unchecked for bare-UUID identity leaks."
    )


@pytest.mark.anyio
async def test_no_tool_response_carries_a_bare_uuid_identity(seeded_entity) -> None:
    server = build_mcp_server()
    registered = {tool.name for tool in await server.list_tools()}
    assert registered == set(_TOOL_CALLS), "tool coverage drifted; see test_every_registered_tool_is_covered"

    violations: list[str] = []
    resolved_sightings = 0
    for name in sorted(registered):
        _content, structured = await server.call_tool(name, _TOOL_CALLS[name])  # type: ignore[misc]
        payload = json.loads(json.dumps(structured, default=str))
        for path, field, value in _walk(payload, ""):
            if field in _EXEMPT_IDENTITY_FIELDS:
                continue
            if _BARE_UUID.match(value):
                violations.append(f"{name}: {path} = {value}")
            if value == seeded_entity["legacy_id"]:
                resolved_sightings += 1

    assert not violations, "MCP responses carry bare-UUID identity fields:\n  " + "\n  ".join(violations)
    # Anti-vacuous: a guard that finds nothing because the seeded row never reached a
    # response would pass over any leak at all. The seeded issuer must come back
    # TRANSLATED at least once -- it entered mart as a UUID.
    assert resolved_sightings > 0, (
        f"no response carried the seeded issuer's resolved identity "
        f"{seeded_entity['legacy_id']}; the guard scanned nothing that could have leaked"
    )
