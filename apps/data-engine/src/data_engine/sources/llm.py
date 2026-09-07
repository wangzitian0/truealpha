"""The filing-extraction model provider as a gated source (#70 scope 1–2, #735 B1).

Precision is the model's half: recall enumerates every stated headcount with its sentence,
and when a filing states several distinct company-wide figures a judgement is needed. The
model gets the enumerated candidates — never the whole filing — and must answer with one
of them or with null; a value that is not a candidate is refused, so the model can choose
but cannot invent (init.md rule 3 in spirit: the fact stays anchored to a verbatim span).

Every call is an append-only invocation record (init.md §9): provider, model, base URL host,
the instruction/schema digests, request and response digests, token cost, and the decision.
A second run for the same (subject, accession, instructions, model) REPLAYS the stored
decision without calling the provider. The call itself goes through the ledger
(`record_call`, rule 6) with its token cost, under the `filing-extraction-model` seat.
"""

from __future__ import annotations

import hashlib
import json
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

from truealpha_contracts.common import canonical_sha256

from data_engine.config import settings
from data_engine.sources import gateway
from data_engine.sources.gateway import record_call

SOURCE = "filing-extraction-model"
ENDPOINT = "chat.completions"
PROMPT_VERSION = "headcount-select:v2"  # v1 named the schema without showing it; every answer came back null

INSTRUCTIONS = (
    "You select the company-wide total employee headcount from candidate sentences taken "
    "from an issuer's annual filing. Rules: choose exactly one candidate whose figure states "
    "the total workforce of the whole issuer as of the filing's stated date. Counts for a "
    "department, segment, subsidiary, region, or for part-time, temporary or contractor "
    "workers are NOT the total. A holding company's 'no employees' statement is not the total "
    "when a subsidiaries-wide figure exists. If no candidate states the company-wide total, "
    "answer null. Respond ONLY with a JSON object of exactly this shape: "
    '{"value": <the chosen candidate\'s integer, or null>, "candidate_index": <its [index], or null>, '
    '"reason": <one short sentence>}'
)
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "value": {"type": ["integer", "null"]},
        "candidate_index": {"type": ["integer", "null"]},
        "reason": {"type": "string"},
    },
    "required": ["value", "candidate_index", "reason"],
}
PROMPT_SHA256 = hashlib.sha256((PROMPT_VERSION + "\n" + INSTRUCTIONS).encode()).hexdigest()
SCHEMA_SHA256 = canonical_sha256(RESPONSE_SCHEMA)

Transport = Callable[[str, dict[str, str], bytes], tuple[int, bytes]]


class ModelNotConfigured(RuntimeError):
    """No provider seated (empty LLM_API_KEY): the caller keeps the cell deferred."""


@dataclass(frozen=True)
class Candidate:
    value: int
    sentence: str


@dataclass(frozen=True)
class ModelSelection:
    value: int | None
    candidate_index: int | None
    reason: str
    model: str
    provider: str
    prompt_sha256: str
    request_sha256: str
    response_sha256: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    invocation_id: str
    replayed: bool
    # What the provider says it answered with (response.model). The coding plan routes
    # legacy names to its current models — glm-4.7 was served by glm-5.3-flash — so the
    # extractor names the served model, and `model` stays the requested name that is part
    # of the request digest.
    served_model: str | None = None

    @property
    def extractor(self) -> str:
        return f"model:{self.served_model or self.model}:{self.prompt_sha256[:12]}"


def is_configured() -> bool:
    return bool(settings.llm_api_key)


def build_request(issuer_label: str, form: str, candidates: Sequence[Candidate], *, model: str) -> dict[str, Any]:
    lines = [f"[{i}] {c.value:,}: {c.sentence}" for i, c in enumerate(candidates)]
    user = f"Issuer: {issuer_label}, form {form}.\nCandidates:\n" + "\n".join(lines)
    return {
        "model": model,
        "messages": [{"role": "system", "content": INSTRUCTIONS}, {"role": "user", "content": user}],
        "max_tokens": 300,
        "temperature": 0,
        # Deterministic decoding settings are part of the invocation identity (§9); the
        # provider's reasoning mode is disabled so the answer is the JSON, not a trace.
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
    }


def _gateway_transport(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
    """The provider call through the external call ledger (rule 6). Nested inside the
    `record_call` block in `select_headcount`, so it enriches that one row — status,
    digest, URI — rather than emitting a second."""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    status, response = gateway.urlopen(SOURCE, ENDPOINT, request, caller="llm", timeout=60, cost=0)
    return int(status or 0), response


def _replay(connection: Any, *, cik: int, accession: str, model: str, request_sha256: str) -> ModelSelection | None:
    """An identical prior ask — same subject, filing, instructions, candidates, decoding
    settings and model, i.e. the same request digest — that the provider answered (status
    < 400). A changed candidate set or schema is a different request and is asked afresh;
    a vendor error is recorded but never replayed as an answer (review on #754)."""
    row = connection.execute(
        """
        select invocation_id, decision, response_sha256, prompt_tokens, completion_tokens, request_sha256, provider,
               served_model
        from staging.model_invocations
        where subject_cik = %s and accession = %s and request_sha256 = %s and model = %s
          and status_code is not null and status_code < 400
        order by id desc limit 1
        """,
        (cik, accession, request_sha256, model),
    ).fetchone()
    if row is None:
        return None
    decision = row[1] if isinstance(row[1], dict) else json.loads(row[1])
    return ModelSelection(
        value=decision.get("value"),
        candidate_index=decision.get("candidate_index"),
        reason=str(decision.get("reason", "")),
        model=model,
        provider=row[6],
        prompt_sha256=PROMPT_SHA256,
        request_sha256=row[5],
        response_sha256=row[2],
        prompt_tokens=row[3],
        completion_tokens=row[4],
        invocation_id=row[0],
        replayed=True,
        served_model=row[7],
    )


def select_headcount(
    connection: Any | None,
    *,
    cik: int,
    accession: str,
    form: str,
    issuer_label: str,
    candidates: Sequence[Candidate],
    caller: str,
    standard: str = "employees_total",
    persist: bool = True,
    transport: Transport | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> ModelSelection:
    """Ask the seated model to choose among enumerated candidates; record the invocation.

    `persist=False` (probe mode) neither replays nor records an invocation — it only asks,
    and the call still lands in the ledger. With a connection and `persist=True`, every ask
    is recorded (answers and vendor errors alike) and an identical prior ANSWERED ask is
    replayed instead of re-asked (§9: replay never silently calls the model again).
    """
    if not is_configured():
        raise ModelNotConfigured("LLM_API_KEY is not set; no provider seated (#70 scope 1)")
    model = settings.llm_model
    request_body = build_request(issuer_label, form, candidates, model=model)
    request_bytes = json.dumps(request_body, sort_keys=True, ensure_ascii=False).encode()
    request_sha256 = hashlib.sha256(request_bytes).hexdigest()
    if persist and connection is not None:
        replayed = _replay(connection, cik=cik, accession=accession, model=model, request_sha256=request_sha256)
        if replayed is not None:
            return replayed

    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {settings.llm_api_key}", "Content-Type": "application/json"}
    started_at = now()
    usage: dict[str, Any] = {}
    payload: dict[str, Any] = {}
    with record_call(SOURCE, ENDPOINT, caller=caller, request_uri=url, cost=0) as call:
        status, body = (transport or _gateway_transport)(url, headers, request_bytes)
        call.observe(status_code=status, body=body)
        if status >= 400:
            call.fail(f"HTTP {status}: {body[:200]!r}")
        else:
            payload = json.loads(body.decode())
            usage = payload.get("usage") or {}
            call.cost = Decimal(int(usage.get("total_tokens") or 0))
    completed_at = now()
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    served_model = str(payload["model"]) if payload.get("model") else None
    decision: dict[str, Any]
    if status >= 400:
        # A vendor error is still an invocation that happened: recorded with its status and
        # body as a refusal, excluded from replay so the next run asks again.
        decision = {"value": None, "candidate_index": None, "reason": f"HTTP {status}: {body[:200]!r}"}
    else:
        content = ((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        decision = _parse_decision(content, candidates)
    response_sha256 = hashlib.sha256(body).hexdigest()
    invocation_id = "model-invocation:" + canonical_sha256(
        {
            "provider": settings.llm_provider,
            "model": model,
            "prompt_sha256": PROMPT_SHA256,
            "request_sha256": request_sha256,
            "response_sha256": response_sha256,
            "started_at": started_at.isoformat(),
        }
    )
    selection = ModelSelection(
        value=decision["value"],
        candidate_index=decision["candidate_index"],
        reason=decision["reason"],
        model=model,
        provider=settings.llm_provider,
        prompt_sha256=PROMPT_SHA256,
        request_sha256=request_sha256,
        response_sha256=response_sha256,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        invocation_id=invocation_id,
        replayed=False,
        served_model=served_model,
    )
    if persist and connection is not None:
        connection.execute(
            """
            insert into staging.model_invocations
                (invocation_id, provider, model, base_url_host, standard, subject_cik, accession,
                 prompt_version, prompt_sha256, schema_sha256, request_sha256, response_sha256, status_code,
                 prompt_tokens, completion_tokens, cost, decision, request, response, started_at, completed_at,
                 served_model)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s,
                    %s)
            on conflict (invocation_id) do nothing
            """,
            (
                invocation_id,
                settings.llm_provider,
                model,
                urlparse(url).hostname or "",
                standard,
                cik,
                accession,
                PROMPT_VERSION,
                PROMPT_SHA256,
                SCHEMA_SHA256,
                request_sha256,
                response_sha256,
                status,
                prompt_tokens,
                completion_tokens,
                Decimal(int(usage.get("total_tokens") or 0)),
                json.dumps(decision, sort_keys=True),
                json.dumps(request_body, sort_keys=True, ensure_ascii=False),
                _jsonb_or_null(body),
                started_at,
                completed_at,
                served_model,
            ),
        )
    if status >= 400:
        raise RuntimeError(f"{SOURCE}: HTTP {status}: {body[:200]!r}")
    return selection


def _jsonb_or_null(body: bytes) -> str | None:
    """The response column is jsonb: a non-JSON vendor body (an HTML error page) is kept
    only through its digest and the ledger's error text, not forced into the column."""
    try:
        return json.dumps(json.loads(body.decode()), ensure_ascii=False)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _parse_decision(content: str, candidates: Sequence[Candidate]) -> dict[str, Any]:
    """The model may choose, never invent: a value that is not a candidate is a refusal."""
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return {"value": None, "candidate_index": None, "reason": f"unparseable model answer: {content[:120]!r}"}
    value = parsed.get("value")
    index = parsed.get("candidate_index")
    reason = str(parsed.get("reason", ""))[:400]
    if value is None:
        return {"value": None, "candidate_index": None, "reason": reason or "model answered null"}
    try:
        value = int(value)
    except (TypeError, ValueError):
        return {"value": None, "candidate_index": None, "reason": f"non-integer model value {value!r}"}
    matching = [i for i, c in enumerate(candidates) if c.value == value]
    if not matching:
        return {"value": None, "candidate_index": None, "reason": f"model chose {value}, which is not a candidate"}
    chosen = index if isinstance(index, int) and index in matching else matching[0]
    return {"value": value, "candidate_index": chosen, "reason": reason}
