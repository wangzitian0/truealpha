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

`as_selector()` at the bottom adapts `select_headcount` to the shared extraction
primitive's `Selector` protocol (`libs/factors/shared/extraction.py`, #769) for a caller
that only needs "chosen value or None" in the primitive's own vocabulary; every ledger,
replay, and persistence behaviour above is unchanged and still reached through it.
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

from factors.shared import extraction as extraction_primitive
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


@dataclass(frozen=True)
class ModelTask:
    """One bound decision: what is asked, in what shape, under whose identity.

    The invocation table has always had `prompt_version`, `prompt_sha256` and
    `schema_sha256` columns — the plane was generic while the module that wrote to it held
    exactly one prompt at module scope. A second decision (segment-theme classification,
    #772) is what turns that from tidy into wrong, so the prompt becomes a value.

    Bound as a whole because §9's replay contract is over the whole ask: the instructions,
    the schema, the decoding settings and the model together are the identity, and a task
    that changed one of them silently would replay an answer to a different question.
    """

    prompt_version: str
    instructions: str
    response_schema: dict[str, Any]
    #: Enough for a short JSON answer. A classification over a handful of segments needs
    #: more room than a single chosen integer, so it is per task rather than global.
    max_tokens: int = 300

    @property
    def prompt_sha256(self) -> str:
        return hashlib.sha256((self.prompt_version + "\n" + self.instructions).encode()).hexdigest()

    @property
    def schema_sha256(self) -> str:
        return canonical_sha256(self.response_schema)


HEADCOUNT_TASK = ModelTask(prompt_version=PROMPT_VERSION, instructions=INSTRUCTIONS, response_schema=RESPONSE_SCHEMA)


@dataclass(frozen=True)
class ModelInvocation:
    """What one ask produced, before any task-specific reading of it.

    `decision` is the parsed answer in the task's own vocabulary; everything else is the
    §9 identity that is the same for every task.
    """

    decision: dict[str, Any]
    model: str
    provider: str
    prompt_sha256: str
    request_sha256: str
    response_sha256: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    invocation_id: str
    replayed: bool
    served_model: str | None = None

    @property
    def extractor(self) -> str:
        return f"model:{self.served_model or self.model}:{self.prompt_sha256[:12]}"


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


def build_chat_request(task: ModelTask, user_content: str, *, model: str) -> dict[str, Any]:
    """The provider request for any task. Its digest IS the replay key, so everything that
    could change the answer has to be in here."""
    return {
        "model": model,
        "messages": [{"role": "system", "content": task.instructions}, {"role": "user", "content": user_content}],
        "max_tokens": task.max_tokens,
        "temperature": 0,
        # Deterministic decoding settings are part of the invocation identity (§9); the
        # provider's reasoning mode is disabled so the answer is the JSON, not a trace.
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
    }


def build_request(issuer_label: str, form: str, candidates: Sequence[Candidate], *, model: str) -> dict[str, Any]:
    """The headcount task's user content, wrapped by the generic builder above."""
    lines = [f"[{i}] {c.value:,}: {c.sentence}" for i, c in enumerate(candidates)]
    user = f"Issuer: {issuer_label}, form {form}.\nCandidates:\n" + "\n".join(lines)
    return build_chat_request(HEADCOUNT_TASK, user, model=model)


def _gateway_transport(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
    """The provider call through the external call ledger (rule 6). Nested inside the
    `record_call` block in `select_headcount`, so it enriches that one row — status,
    digest, URI — rather than emitting a second."""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    status, response = gateway.urlopen(SOURCE, ENDPOINT, request, caller="llm", timeout=60, cost=0)
    return int(status or 0), response


def _replay(
    connection: Any, *, task: ModelTask, cik: int, accession: str, model: str, request_sha256: str
) -> ModelInvocation | None:
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
    return ModelInvocation(
        decision=decision,
        model=model,
        provider=row[6],
        prompt_sha256=task.prompt_sha256,
        request_sha256=row[5],
        response_sha256=row[2],
        prompt_tokens=row[3],
        completion_tokens=row[4],
        invocation_id=row[0],
        replayed=True,
        served_model=row[7],
    )


def invoke(
    connection: Any | None,
    *,
    task: ModelTask,
    cik: int,
    accession: str,
    user_content: str,
    caller: str,
    standard: str,
    parse: Callable[[str], dict[str, Any]],
    refusal: Callable[[str], dict[str, Any]],
    persist: bool = True,
    transport: Transport | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> ModelInvocation:
    """Ask the seated model one task's question; record the invocation. Task-agnostic.

    `persist=False` (probe mode) neither replays nor records an invocation — it only asks,
    and the call still lands in the ledger. With a connection and `persist=True`, every ask
    is recorded (answers and vendor errors alike) and an identical prior ANSWERED ask is
    replayed instead of re-asked (§9: replay never silently calls the model again).

    `parse` reads the provider's content into the task's own decision shape; `refusal`
    builds that same shape for a vendor error, so a failed ask is still a well-formed
    recorded decision rather than a hole the caller has to special-case.
    """
    if not is_configured():
        raise ModelNotConfigured("LLM_API_KEY is not set; no provider seated (#70 scope 1)")
    model = settings.llm_model
    request_body = build_chat_request(task, user_content, model=model)
    request_bytes = json.dumps(request_body, sort_keys=True, ensure_ascii=False).encode()
    request_sha256 = hashlib.sha256(request_bytes).hexdigest()
    if persist and connection is not None:
        replayed = _replay(
            connection, task=task, cik=cik, accession=accession, model=model, request_sha256=request_sha256
        )
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
    if status >= 400:
        # A vendor error is still an invocation that happened: recorded with its status and
        # body as a refusal, excluded from replay so the next run asks again.
        decision = refusal(f"HTTP {status}: {body[:200]!r}")
    else:
        content = ((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        decision = parse(content)
    response_sha256 = hashlib.sha256(body).hexdigest()
    invocation_id = "model-invocation:" + canonical_sha256(
        {
            "provider": settings.llm_provider,
            "model": model,
            "prompt_sha256": task.prompt_sha256,
            "request_sha256": request_sha256,
            "response_sha256": response_sha256,
            "started_at": started_at.isoformat(),
        }
    )
    invocation = ModelInvocation(
        decision=decision,
        model=model,
        provider=settings.llm_provider,
        prompt_sha256=task.prompt_sha256,
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
                task.prompt_version,
                task.prompt_sha256,
                task.schema_sha256,
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
    return invocation


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

    The ledger, replay and persistence behaviour is `invoke`'s (above), shared with every
    other task. What is headcount's own is the two things a task owns: how the candidates
    are written into the question, and how the answer is read back — here, the rule that
    the model may CHOOSE but never invent.
    """
    lines = [f"[{i}] {c.value:,}: {c.sentence}" for i, c in enumerate(candidates)]
    user = f"Issuer: {issuer_label}, form {form}.\nCandidates:\n" + "\n".join(lines)
    invocation = invoke(
        connection,
        task=HEADCOUNT_TASK,
        cik=cik,
        accession=accession,
        user_content=user,
        caller=caller,
        standard=standard,
        parse=lambda content: _parse_decision(content, candidates),
        refusal=lambda detail: {"value": None, "candidate_index": None, "reason": detail},
        persist=persist,
        transport=transport,
        now=now,
    )
    decision = invocation.decision
    return ModelSelection(
        value=decision.get("value"),
        candidate_index=decision.get("candidate_index"),
        reason=str(decision.get("reason", "")),
        model=invocation.model,
        provider=invocation.provider,
        prompt_sha256=invocation.prompt_sha256,
        request_sha256=invocation.request_sha256,
        response_sha256=invocation.response_sha256,
        prompt_tokens=invocation.prompt_tokens,
        completion_tokens=invocation.completion_tokens,
        invocation_id=invocation.invocation_id,
        replayed=invocation.replayed,
        served_model=invocation.served_model,
    )


#: #772 / init.md §7 module 6: "LLM-assisted semantic classification of segment revenue".
#: The theme is NOT in these instructions — it travels in the user content, so one task
#: identity covers "how to judge a segment against a stated theme" while the theme itself
#: still enters `request_sha256` and therefore the replay key. A per-theme prompt would
#: make every theme a new prompt version and every wording tweak a migration.
SEGMENT_THEME_PROMPT_VERSION = "segment-theme:v1"
SEGMENT_THEME_INSTRUCTIONS = (
    "You judge whether each of an issuer's reportable segments belongs to a stated investment "
    "theme. You are given the theme, the definition of what counts as in-theme, and the "
    "segments as the issuer's own filing names them. Judge each segment on the theme "
    "definition alone. Answer true when the segment's revenue is predominantly in the theme, "
    "false when it predominantly is not, and null when the segment name does not carry enough "
    "information to decide — null is a correct answer and is preferred over a guess. Return "
    "one verdict per segment, using the [index] given. Respond ONLY with a JSON object of "
    'exactly this shape: {"verdicts": [{"index": <int>, "in_theme": <true|false|null>, '
    '"reason": <one short sentence>}]}'
)
SEGMENT_THEME_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "in_theme": {"type": ["boolean", "null"]},
                    "reason": {"type": "string"},
                },
                "required": ["index", "in_theme", "reason"],
            },
        }
    },
    "required": ["verdicts"],
}
SEGMENT_THEME_TASK = ModelTask(
    prompt_version=SEGMENT_THEME_PROMPT_VERSION,
    instructions=SEGMENT_THEME_INSTRUCTIONS,
    response_schema=SEGMENT_THEME_SCHEMA,
    # One verdict per segment plus a sentence each; issuers report two to six segments.
    max_tokens=900,
)


@dataclass(frozen=True)
class ThemeClassification:
    """One verdict per segment, in the order the segments were given.

    `verdicts[i] is None` means the classifier declined or never answered for that segment.
    That is deliberately NOT `False`: a declined segment is unclassified revenue that lowers
    confidence in the share, while `False` is a judgement that lowers the share itself, and
    `factors.base.theme_purity` counts them into different masses. Collapsing them would let
    a silent model look like a confident "not in the theme" — and would make an issuer whose
    segments the model could not read look purer or dirtier than the evidence supports.
    """

    verdicts: tuple[bool | None, ...]
    reasons: tuple[str, ...]
    invocation: ModelInvocation

    @property
    def extractor(self) -> str:
        return self.invocation.extractor


def _parse_theme_verdicts(content: str, segments: Sequence[str]) -> dict[str, Any]:
    """Read the model's verdicts, defaulting every unanswered segment to unclassified.

    The model may judge, never invent: an index outside the segment list is dropped rather
    than shifted onto a neighbour, and a segment the answer skips stays `None`. A parser
    that packed verdicts positionally would silently attribute one segment's judgement to
    another whenever the model returned a short list.
    """
    verdicts: list[bool | None] = [None] * len(segments)
    reasons: list[str] = [""] * len(segments)
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return {
            "verdicts": verdicts,
            "reasons": [f"unparseable model answer: {content[:120]!r}"] * len(segments),
        }
    for item in parsed.get("verdicts") or []:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        if not isinstance(index, int) or not 0 <= index < len(segments):
            continue
        value = item.get("in_theme")
        verdicts[index] = value if isinstance(value, bool) else None
        reasons[index] = str(item.get("reason", ""))[:400]
    return {"verdicts": verdicts, "reasons": reasons}


def classify_segments(
    connection: Any | None,
    *,
    cik: int,
    accession: str,
    issuer_label: str,
    theme: str,
    inclusion: str,
    segments: Sequence[str],
    caller: str,
    standard: str = "segment_revenue",
    persist: bool = True,
    transport: Transport | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> ThemeClassification:
    """Ask the seated model which of an issuer's segments belong to a theme.

    The model sees the segment NAMES and the theme definition — never the revenue. That is
    not an economy: a classifier that can see which segment is the big one can be graded by
    how flattering its answer is, and the whole point of q6 is that the share is computed
    from a judgement made without knowing what it would produce.

    Ledger, replay and the invocation record are `invoke`'s, shared with the headcount task.
    """
    lines = [f"[{i}] {name}" for i, name in enumerate(segments)]
    user = f"Issuer: {issuer_label}.\nTheme: {theme}.\nIn-theme means: {inclusion}\nSegments:\n" + "\n".join(lines)
    invocation = invoke(
        connection,
        task=SEGMENT_THEME_TASK,
        cik=cik,
        accession=accession,
        user_content=user,
        caller=caller,
        standard=standard,
        parse=lambda content: _parse_theme_verdicts(content, segments),
        refusal=lambda detail: {"verdicts": [None] * len(segments), "reasons": [detail] * len(segments)},
        persist=persist,
        transport=transport,
        now=now,
    )
    decision = invocation.decision
    raw = decision.get("verdicts") or []
    verdicts = tuple((raw[i] if i < len(raw) and isinstance(raw[i], bool) else None) for i in range(len(segments)))
    raw_reasons = decision.get("reasons") or []
    reasons = tuple(str(raw_reasons[i]) if i < len(raw_reasons) else "" for i in range(len(segments)))
    return ThemeClassification(verdicts=verdicts, reasons=reasons, invocation=invocation)


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


def as_selection(selection: ModelSelection) -> extraction_primitive.Selection | None:
    """Adapt this module's `ModelSelection` — its ledger/replay identity, token cost, and
    the served-model quirk (a requested `model` alias may be served under a different
    name; see `ModelSelection.extractor`) — to the shared primitive's minimal `Selection`.

    `None` for a decline, exactly like the primitive's own `select_single_candidate`:
    a caller that only wants "chosen value or nothing to choose" does not need to branch
    on this module's richer refusal detail (`selection.reason` is still on `ModelSelection`
    for a caller that does).
    """
    if selection.value is None or selection.candidate_index is None:
        return None
    return extraction_primitive.Selection(
        value=selection.value,
        candidate_index=selection.candidate_index,
        extractor=selection.extractor,
        reason=selection.reason,
        invocation_id=selection.invocation_id,
    )


def _integral_value(value: int | float) -> int:
    """`Candidate.value` on the shared primitive is `int | float` — headcount is always
    integral, but a silent `int(...)` truncation would turn a hypothetical 42.7 into 42
    without complaint (Copilot review on #782). This module's own `Candidate.value` is
    `int`, so a non-integral value here means the caller handed this selector a candidate
    it was never meant for; that is a caller bug, not a value to guess at."""
    if isinstance(value, int):
        return value
    if value.is_integer():
        return int(value)
    raise ValueError(f"as_selector() received a non-integral candidate value {value!r}; headcount candidates are int")


def as_selector(
    connection: Any | None,
    *,
    cik: int,
    accession: str,
    form: str,
    issuer_label: str,
    caller: str,
    standard: str = "employees_total",
    persist: bool = True,
    transport: Transport | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> extraction_primitive.Selector:
    """Bind this call's ledger/replay context and return a `Selector`-conformant callable
    (`libs/factors/shared/extraction.py`'s protocol) for a caller that only has the shared
    primitive's `Candidate` sequence — this is the "model-backed selector plugs in without
    the library importing data_engine" half of #769: the primitive depends only on the
    protocol shape, and this factory is where a concrete implementation is bound to it.

    `select_headcount`'s own ledger/replay/persistence behaviour (see above) is reached
    unchanged through the returned closure; nothing about it is reimplemented here.
    """

    def select(candidates: Sequence[extraction_primitive.Candidate]) -> extraction_primitive.Selection | None:
        local_candidates = [Candidate(value=_integral_value(c.value), sentence=c.sentence) for c in candidates]
        selection = select_headcount(
            connection,
            cik=cik,
            accession=accession,
            form=form,
            issuer_label=issuer_label,
            candidates=local_candidates,
            caller=caller,
            standard=standard,
            persist=persist,
            transport=transport,
            now=now,
        )
        return as_selection(selection)

    return select
