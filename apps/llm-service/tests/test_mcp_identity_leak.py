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

_DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/truealpha"

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
    "strategy_run": {"request": {"strategy_id": "large_model_value_v0"}},
    # theme_ranking, not company: a ranking report projects EVERY decision of the run into
    # a subject, so its subject ids come from the read path rather than being echoed back
    # from target_entity_ids the test itself supplied. The request needs at least one
    # target id (the DTO rejects an empty tuple), and it is deliberately NOT a UUID: a
    # UUID the test hands in and the report echoes back would be this guard tripping over
    # its own argument, not over a leak.
    "research_report": {
        "request": {
            "report_kind": "theme_ranking",
            "target_entity_ids": ["issuer:ranking-scope-unused"],
            "cutoff_at": _CUTOFF,
        }
    },
    "research_card": {
        "request": {
            "report_kind": "theme_ranking",
            "target_entity_ids": ["issuer:ranking-scope-unused"],
            "cutoff_at": _CUTOFF,
            "card_kind": "ranking",
        }
    },
    # topt_gppe reads the governed capture head, which this transaction does not seed; on a
    # database with no accepted production run it answers a typed Unavailable, and the walk
    # below covers whatever it does answer. Its own cell translation has real coverage in
    # libs/contracts/tests/test_topt_read.py (#954).
    "topt_gppe": {},
}


def _database_url() -> str:
    return os.environ.get("DATABASE_URL", _DEFAULT_DATABASE_URL)


class _BorrowedConnection:
    """Redirects each repository's per-call ``psycopg.connect`` onto the test's own
    transaction; close/commit are withheld so the rollback owns the data. Carries both
    access shapes the repositories use: ``cursor(...)`` and ``execute(...)``."""

    def __init__(self, real: psycopg.Connection) -> None:
        self._real = real

    def __enter__(self) -> _BorrowedConnection:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def cursor(self, **kwargs: Any) -> Any:
        return self._real.cursor(**kwargs)

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
    try:
        connection = psycopg.connect(_database_url(), connect_timeout=3, autocommit=False)
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
        monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: _BorrowedConnection(connection))
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
