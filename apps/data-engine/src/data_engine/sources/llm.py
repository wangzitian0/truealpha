# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import contextvars; import hashlib; import json; import logging; import re; import urllib.request; from collections.abc import Callable, Sequence; from contextlib import nullcontext; from dataclasses import dataclass; from datetime import UTC, datetime; from decimal import Decimal; from functools import partial; from typing import Any; from urllib.parse import urlparse; from factors.shared import extraction as extraction_primitive; from truealpha_contracts.common import canonical_sha256; from truealpha_contracts.research_cards import ( XHS_CARD_INSTRUCTIONS, XHS_CARD_PROMPT_VERSION, ResearchCard, build_card_prompt, extract_html_document, render_card_html, verify_card_html, ); from data_engine.config import settings; from data_engine.datahub.fetch_key import FetchKey; from data_engine.sources import gateway, model_decoding, model_policy; from data_engine.sources.gateway import record_call; logger = logging.getLogger(__name__); SOURCE = model_policy.seat_contract().seat; ENDPOINT, PROMPT_VERSION, MAX_CONTEXT_TOKENS, FINISH_REASON_CUT_OFF = ( "chat.completions", "headcount-select:v2", 1_000_000, "length", ); _CHARS_PER_TOKEN_FLOOR, _COMPACT_REQUEST_BYTE_THRESHOLD = 2.5, 16_384; INSTRUCTIONS = ( "You select the company-wide total employee headcount from candidate sentences taken " "from an issuer's annual filing. Rules: choose exactly one candidate whose figure states " "the total workforce of the whole issuer as of the filing's stated date. Counts for a " "department, segment, subsidiary, region, or for part-time, temporary or contractor " "workers are NOT the total. A holding company's 'no employees' statement is not the total " "when a subsidiaries-wide figure exists. If no candidate states the company-wide total, " "answer null. Respond ONLY with a JSON object of exactly this shape: " '{"value": <the chosen candidate\'s integer, or null>, "candidate_index": <its [index], or null>, ' '"reason": <one short sentence>}' ); RESPONSE_SCHEMA: dict[str, Any] = { "type": "object", "properties": { "value": {"type": ["integer", "null"]}, "candidate_index": {"type": ["integer", "null"]}, "reason": {"type": "string"}, }, "required": ["value", "candidate_index", "reason"], }; PROMPT_SHA256 = hashlib.sha256((PROMPT_VERSION + "\n" + INSTRUCTIONS).encode()).hexdigest(); SCHEMA_SHA256 = canonical_sha256(RESPONSE_SCHEMA)
@dataclass(frozen=True)
class ModelTask:
    prompt_version: str; instructions: str; response_schema: dict[str, Any]; max_tokens: int = 300; timeout_seconds: int = 60; max_context_tokens: int = MAX_CONTEXT_TOKENS; answer_required: bool = True
    @property
    def prompt_sha256(self) -> str: return hashlib.sha256((self.prompt_version + "\n" + self.instructions).encode()).hexdigest()
    @property
    def schema_sha256(self) -> str: return canonical_sha256(self.response_schema)
HEADCOUNT_TASK = ModelTask(prompt_version=PROMPT_VERSION, instructions=INSTRUCTIONS, response_schema=RESPONSE_SCHEMA)
@dataclass(frozen=True)
class ModelInvocation:
    decision: dict[str, Any]; model: str; provider: str; prompt_sha256: str; request_sha256: str; response_sha256: str | None; prompt_tokens: int | None; completion_tokens: int | None; invocation_id: str; replayed: bool; served_model: str | None = None
    @property
    def extractor(self) -> str: return f"model:{self.served_model or self.model}:{self.prompt_sha256[:12]}"
Transport = Callable[[str, dict[str, str], bytes], tuple[int, bytes]]
class ModelNotConfigured(RuntimeError): pass
class ModelHTTPError(RuntimeError):
    def __init__(self, status_code: int, message: str, provider_code: str | None = None) -> None: super().__init__(message); self.status_code, self.provider_code = status_code, provider_code
class ModelCapacityRefused(ModelHTTPError):
    def __init__(self, detail: str) -> None: super().__init__(429, f"{SOURCE}: {detail}")
class ModelOutputTruncated(ModelHTTPError):
    def __init__(self, max_tokens: int) -> None: super().__init__(200, f"{SOURCE}: the answer was cut off at max_tokens {max_tokens}"); self.max_tokens = max_tokens
class ModelContextLimitError(ModelHTTPError):
    def __init__( self, *, cik: int, accession: str, estimated_tokens: int, max_context_tokens: int, detail: str, status_code: int = 413, ) -> None: super().__init__(status_code, detail); self.cik, self.accession, self.estimated_tokens, self.max_context_tokens = ( cik, accession, estimated_tokens, max_context_tokens, )
@dataclass(frozen=True)
class Candidate: value: int; sentence: str
@dataclass(frozen=True)
class ModelSelection:
    value: int | None; candidate_index: int | None; reason: str; model: str; provider: str; prompt_sha256: str; request_sha256: str; response_sha256: str | None; prompt_tokens: int | None; completion_tokens: int | None; invocation_id: str; replayed: bool; served_model: str | None = None
    @property
    def extractor(self) -> str: return f"model:{self.served_model or self.model}:{self.prompt_sha256[:12]}"
def is_configured() -> bool: return bool(settings.llm_api_key)
def build_chat_request( task: ModelTask, user_content: str, *, model: str, decoding: model_decoding.DecodingVersion | None = None ) -> dict[str, Any]: version = decoding or model_decoding.for_environment(gateway.capacity_environment()); return { "model": model, "messages": [{"role": "system", "content": task.instructions}, {"role": "user", "content": user_content}], "max_tokens": task.max_tokens, "temperature": 0, "response_format": {"type": "json_object"}, **version.parameters(), }
def build_request(issuer_label: str, form: str, candidates: Sequence[Candidate], *, model: str) -> dict[str, Any]: user = f"Issuer: {issuer_label}, form {form}.\nCandidates:\n" + "\n".join( f"[{i}] {c.value:,}: {c.sentence}" for i, c in enumerate(candidates) ); return build_chat_request(HEADCOUNT_TASK, user, model=model)
def _request_params(request_body: dict[str, Any], decoding: model_decoding.DecodingVersion) -> dict[str, Any]: thinking, response_format = request_body.get("thinking"), request_body.get("response_format"); return { "model": request_body.get("model"), "decoding_version": decoding.name, "thinking": thinking.get("type") if isinstance(thinking, dict) else None, "reasoning_effort": request_body.get("reasoning_effort"), "max_tokens": request_body.get("max_tokens"), "temperature": request_body.get("temperature"), "response_format": response_format.get("type") if isinstance(response_format, dict) else None, }
_attempt_timeout: contextvars.ContextVar[int] = contextvars.ContextVar( "llm_attempt_timeout", default=ModelTask.timeout_seconds ); _attempt_identity: contextvars.ContextVar[FetchKey] = contextvars.ContextVar("llm_attempt_identity")
def _model_key(standard: str, cik: int, accession: str, request_sha256: str) -> FetchKey:
    if accession: return FetchKey(SOURCE, f"{ENDPOINT}?standard={standard}", f"filing:{cik:010d}:{accession}", request_sha256)
    return FetchKey( SOURCE, f"{ENDPOINT}?standard={standard}", f"seat:{SOURCE}", f"{gateway.utc_day()}:{request_sha256}" )
def _gateway_transport(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]: request = urllib.request.Request(url, data=body, headers=headers, method="POST"); status, response = gateway.urlopen( SOURCE, ENDPOINT, request, identity=_attempt_identity.get(), caller="llm", timeout=_attempt_timeout.get(), cost=0, ); return int(status or 0), response
def _stored_request(request_body: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(messages := request_body.get("messages"), list): return request_body
    compacted: list[dict[str, Any]] = []; changed = False
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "user" and isinstance(content := msg.get("content"), str):
            raw = content.encode("utf-8")
            if len(raw) > _COMPACT_REQUEST_BYTE_THRESHOLD: changed = True; compacted.append( { "role": "user", "content_sha256": hashlib.sha256(raw).hexdigest(), "content_chars": len(content), "content_bytes": len(raw), } ); continue
        compacted.append(msg)
    return {**request_body, "messages": compacted} if changed else request_body
def _without_model(request_body: dict[str, Any]) -> dict[str, Any]: return {k: v for k, v in _stored_request(request_body).items() if k != "model"}
def _estimate_prompt_tokens(task: ModelTask, user_content: str) -> int: return int((len(task.instructions) + len(user_content)) / _CHARS_PER_TOKEN_FLOOR)
_CONTEXT_LIMIT_CODES = frozenset( {"context_length_exceeded", "context_window_exceeded", "tokens_limit_reached", "prompt_too_long"} ); _CONTEXT_LIMIT_PHRASES = ( "context_length_exceeded", "context window", "maximum context length", "prompt is too long", "tokens_limit_reached", "maximum input length", "exceeds the maximum number of tokens", )
def _is_context_limit_response(status: int, body: bytes) -> bool:
    if status == 413: return True
    if status != 400: return False
    text = body.decode("utf-8", errors="ignore")
    try: parsed = json.loads(text)
    except json.JSONDecodeError: parsed = None
    if isinstance(parsed, dict):
        err = parsed["error"] if isinstance(parsed.get("error"), dict) else parsed
        if str(err.get("code") or "").strip().lower() in _CONTEXT_LIMIT_CODES: return True
    lower = text.lower(); return any(p in lower for p in _CONTEXT_LIMIT_PHRASES)
def _replay_sql(versions: int) -> str: same_digest = " or ".join(["(request_sha256 = %s and model = %s)"] * versions); same_question = ", ".join(["%s::jsonb"] * versions); return ( "select invocation_id, decision, response_sha256, prompt_tokens, completion_tokens, request_sha256, " "provider, served_model, model from staging.model_invocations where subject_cik = %s and accession = %s " f"and status_code is not null and status_code < 400 and coalesce(response -> 'choices' -> 0 ->> 'finish_reason', '') <> '{FINISH_REASON_CUT_OFF}' " f"and ({same_digest} or (served_model = %s and (request - 'model') in ({same_question}))) order by id asc limit 1" )
_LOCK_SQL = "select pg_advisory_xact_lock(hashtextextended(%s, 0))"
def _lock_question(connection: Any, *, cik: int, accession: str, question: str) -> None: digest = hashlib.sha256(question.encode()).hexdigest(); connection.execute(_LOCK_SQL, (f"staging.model_invocations:{cik}:{accession}:{digest}",))
def _replay( connection: Any, *, task: ModelTask, cik: int, accession: str, model: str, requests: Sequence[dict[str, Any]] ) -> ModelInvocation | None:
    params = ( cik, accession, *(val for req in requests for val in (_request_sha256(req), model)), model, *(_question(req) for req in requests), )
    if (row := connection.execute(_replay_sql(len(requests)), params).fetchone()) is None: return None
    decision = row[1] if isinstance(row[1], dict) else json.loads(row[1]); return ModelInvocation( decision=decision, model=row[8], provider=row[6], prompt_sha256=task.prompt_sha256, request_sha256=row[5], response_sha256=row[2], prompt_tokens=row[3], completion_tokens=row[4], invocation_id=row[0], replayed=True, served_model=row[7], )
def invoke( connection: Any | None, *, task: ModelTask, cik: int, accession: str, user_content: str, caller: str, standard: str, parse: Callable[[str], dict[str, Any]], refusal: Callable[[str], dict[str, Any]], persist: bool = True, transport: Transport | None = None, now: Callable[[], datetime] = lambda: datetime.now(UTC), ) -> ModelInvocation:
    if not is_configured():
        raise ModelNotConfigured(f"{settings.llm_api_key_env} is not set; no provider seated (#70 scope 1)")
    cik = cik if cik > 0 else ((int(hashlib.sha256(accession.encode()).hexdigest()[:8], 16) & 0x7FFFFFFF) or 1); model, decoding = settings.llm_model, model_decoding.for_environment(gateway.capacity_environment()); request_body = build_chat_request(task, user_content, model=model, decoding=decoding); ask = partial( _ask, task=task, model=model, decoding=decoding, request_body=request_body, cik=cik, accession=accession, caller=caller, standard=standard, parse=parse, refusal=refusal, transport=transport, now=now, )
    if not persist or connection is None: invocation, error = ask(None)
    else:
        with connection.transaction() if getattr(connection, "autocommit", False) else nullcontext():
            known = [build_chat_request(task, user_content, model=model, decoding=v) for v in model_decoding.KNOWN]; _lock_question(connection, cik=cik, accession=accession, question=_question(known[0]))
            if ( replayed := _replay(connection, task=task, cik=cik, accession=accession, model=model, requests=known) ) is not None: return replayed
            invocation, error = ask(connection)
    if error is not None: raise error
    return invocation
def _question(request_body: dict[str, Any]) -> str: return json.dumps(_without_model(request_body), sort_keys=True, ensure_ascii=False)
def _request_sha256(request_body: dict[str, Any]) -> str: return hashlib.sha256(_request_bytes(request_body)).hexdigest()
def _request_bytes(request_body: dict[str, Any]) -> bytes: return json.dumps(request_body, sort_keys=True, ensure_ascii=False).encode()
@dataclass(frozen=True)
class _ProviderAnswer: status: int; body: bytes; payload: dict[str, Any]; usage: dict[str, Any]; provider_code: str | None
def _call_provider( *, url: str, headers: dict[str, str], request_bytes: bytes, request_body: dict[str, Any], decoding: model_decoding.DecodingVersion, timeout_seconds: int, answer_required: bool, caller: str, identity: FetchKey, transport: Transport | None, ) -> _ProviderAnswer:
    token, id_token = _attempt_timeout.set(timeout_seconds), _attempt_identity.set(identity)
    try: return _retry_loop( budget=model_policy.RetryBudget(), pacer=model_policy.pacer_for(gateway.capacity_environment()), send=transport or _gateway_transport, url=url, headers=headers, request_bytes=request_bytes, request_params=_request_params(request_body, decoding), caller=caller, identity=identity, answer_required=answer_required, )
    finally: _attempt_identity.reset(id_token); _attempt_timeout.reset(token)
def _retry_loop( *, budget: model_policy.RetryBudget, pacer: model_policy.ModelPacer | None, send: Transport, url: str, headers: dict[str, str], request_bytes: bytes, request_params: dict[str, Any], caller: str, identity: FetchKey, answer_required: bool, ) -> _ProviderAnswer:
    last: _ProviderAnswer | None = None
    while True:
        attempt = budget.begin_attempt()
        try: last = _attempt( attempt=attempt, pacer=pacer, remaining_seconds=budget.remaining_seconds, send=send, url=url, headers=headers, request_bytes=request_bytes, request_params=request_params, caller=caller, identity=identity, answer_required=answer_required, )
        except gateway.CapacityExceeded as refusal:
            if last is None: raise ModelCapacityRefused(refusal.reason) from refusal
            return last
        wait = None if last.status < 400 else budget.wait_before_next(last.status, last.provider_code)
        if wait is None: return last
        logger.warning( "model call answered HTTP %s (provider code %s) on attempt %s; waiting %.1f s", last.status, last.provider_code, attempt, wait, ); model_policy.sleep(wait)
def _attempt( *, attempt: int, pacer: model_policy.ModelPacer | None, remaining_seconds: Callable[[], float], send: Transport, url: str, headers: dict[str, str], request_bytes: bytes, request_params: dict[str, Any], caller: str, identity: FetchKey, answer_required: bool, ) -> _ProviderAnswer:
    payload: dict[str, Any] = {}; usage: dict[str, Any] = {}; provider_code: str | None = None
    with ( pacer.admitted(remaining_seconds) if pacer is not None else nullcontext(), record_call(SOURCE, ENDPOINT, identity=identity, caller=caller, request_uri=url, cost=0, admit=False) as call, ):
        call.attempt, call.request_params = attempt, request_params; status, body = send(url, headers, request_bytes); call.observe(status_code=status, body=body)
        if status >= 400: provider_code = model_policy.provider_code(body); call.provider_code = provider_code; call.fail(f"HTTP {status}: {body[:200]!r}")
        else:
            payload = json.loads(body.decode()); usage = payload.get("usage") or {}; call.cost = Decimal(int(usage.get("total_tokens") or 0)); call.completion_tokens = _token_count(usage.get("completion_tokens")); call.reasoning_tokens = _token_count((usage.get("completion_tokens_details") or {}).get("reasoning_tokens"))
            if answer_required and _cut_off(payload): call.fail(f"finish_reason={FINISH_REASON_CUT_OFF}: the answer was cut at max_tokens")
    return _ProviderAnswer(status, body, payload, usage, provider_code)
def _cut_off(payload: dict[str, Any]) -> bool: return ((payload.get("choices") or [{}])[0]).get("finish_reason") == FINISH_REASON_CUT_OFF
def _answer_decision( payload: dict[str, Any], max_tokens: int, answer_required: bool, parse: Callable[[str], dict[str, Any]], refusal: Callable[[str], dict[str, Any]], ) -> dict[str, Any]:
    if answer_required and _cut_off(payload): return refusal(f"truncated: finish_reason={FINISH_REASON_CUT_OFF} at max_tokens {max_tokens}")
    return parse(((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
def _token_count(value: Any) -> int | None: return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
def _ask( connection: Any | None, *, task: ModelTask, model: str, decoding: model_decoding.DecodingVersion, request_body: dict[str, Any], cik: int, accession: str, caller: str, standard: str, parse: Callable[[str], dict[str, Any]], refusal: Callable[[str], dict[str, Any]], transport: Transport | None, now: Callable[[], datetime], ) -> tuple[ModelInvocation, ModelHTTPError | None]:
    request_bytes = _request_bytes(request_body); request_sha256 = hashlib.sha256(request_bytes).hexdigest(); stored_request = _stored_request(request_body); url = settings.llm_base_url.rstrip("/") + "/chat/completions"; headers = {"Authorization": f"Bearer {settings.llm_api_key}", "Content-Type": "application/json"}; started_at = now(); usage: dict[str, Any] = {}; payload: dict[str, Any] = {}; user_text = next( ( str(m.get("content", "")) for m in request_body.get("messages", []) if isinstance(m, dict) and m.get("role") == "user" ), "", ); estimated_tokens = _estimate_prompt_tokens(task, user_text); context_limit_exceeded = estimated_tokens > task.max_context_tokens; provider_code: str | None = None; prompt_tokens: int | None = None; completion_tokens: int | None = None; served_model: str | None = None
    if context_limit_exceeded: status = 413; limit_msg = ( f"context_window_exceeded: cik={cik} accession={accession} standard={standard} " f"estimated_tokens={estimated_tokens} exceeds max_context_tokens={task.max_context_tokens} (chars={len(user_text)})" ); logger.error("LLM context ceiling exceeded (>1M tokens): %s", limit_msg); body = json.dumps( { "error": { "code": "context_window_exceeded", "message": limit_msg, "estimated_tokens": estimated_tokens, "max_context_tokens": task.max_context_tokens, } } ).encode("utf-8"); prompt_tokens, completion_tokens, served_model, decision = estimated_tokens, 0, None, refusal(limit_msg)
    else:
        answer = _call_provider( url=url, headers=headers, request_bytes=request_bytes, request_body=request_body, decoding=decoding, timeout_seconds=task.timeout_seconds, answer_required=task.answer_required, caller=caller, identity=_model_key(standard, cik, accession, request_sha256), transport=transport, ); status, body, payload, usage, provider_code = ( answer.status, answer.body, answer.payload, answer.usage, answer.provider_code, ); prompt_tokens, completion_tokens = usage.get("prompt_tokens"), usage.get("completion_tokens"); served_model = str(payload["model"]) if payload.get("model") else None
        if status >= 400:
            if _is_context_limit_response(status, body): context_limit_exceeded = True; limit_msg = f"context_window_exceeded: cik={cik} accession={accession} standard={standard} HTTP {status}: {body[:200]!r}"; logger.error("LLM provider rejected prompt for exceeding context window: %s", limit_msg); decision = refusal(limit_msg)
            else: decision = refusal(f"HTTP {status}: {body[:200]!r}")
        else: decision = _answer_decision(payload, request_body["max_tokens"], task.answer_required, parse, refusal)
    completed_at, response_sha256 = now(), hashlib.sha256(body).hexdigest(); invocation_id = "model-invocation:" + canonical_sha256( { "provider": settings.llm_provider, "model": model, "prompt_sha256": task.prompt_sha256, "request_sha256": request_sha256, "response_sha256": response_sha256, "started_at": started_at.isoformat(), **({"served_model": served_model} if served_model else {}), } ); invocation = ModelInvocation( decision, model, settings.llm_provider, task.prompt_sha256, request_sha256, response_sha256, prompt_tokens, completion_tokens, invocation_id, False, served_model, )
    if connection is not None: connection.execute( "insert into staging.model_invocations (invocation_id, provider, model, base_url_host, standard, subject_cik, accession, prompt_version, prompt_sha256, schema_sha256, request_sha256, response_sha256, status_code, prompt_tokens, completion_tokens, cost, decision, request, response, started_at, completed_at, served_model) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, %s) on conflict (invocation_id) do nothing", ( invocation_id, settings.llm_provider, model, urlparse(url).hostname or "", standard, cik, accession, task.prompt_version, task.prompt_sha256, task.schema_sha256, request_sha256, response_sha256, status, prompt_tokens, completion_tokens, Decimal(int(usage.get("total_tokens") or 0)), json.dumps(decision, sort_keys=True), json.dumps(stored_request, sort_keys=True, ensure_ascii=False), _jsonb_or_null(body), started_at, completed_at, served_model, ), )
    if context_limit_exceeded: return invocation, ModelContextLimitError( cik=cik, accession=accession, estimated_tokens=estimated_tokens, max_context_tokens=task.max_context_tokens, detail=f"{SOURCE}: context_window_exceeded (cik={cik}, accession={accession}, estimated_tokens={estimated_tokens})", status_code=status, )
    if status >= 400: return invocation, ModelHTTPError(status, f"{SOURCE}: HTTP {status}: {body[:200]!r}", provider_code)
    if task.answer_required and _cut_off(payload): return invocation, ModelOutputTruncated(request_body["max_tokens"])
    return invocation, None
def select_headcount( connection: Any | None, *, cik: int, accession: str, form: str, issuer_label: str, candidates: Sequence[Candidate], caller: str, standard: str = "employees_total", persist: bool = True, transport: Transport | None = None, now: Callable[[], datetime] = lambda: datetime.now(UTC), ) -> ModelSelection: user = f"Issuer: {issuer_label}, form {form}.\nCandidates:\n" + "\n".join( f"[{i}] {c.value:,}: {c.sentence}" for i, c in enumerate(candidates) ); inv = invoke( connection, task=HEADCOUNT_TASK, cik=cik, accession=accession, user_content=user, caller=caller, standard=standard, parse=lambda content: _parse_decision(content, candidates), refusal=lambda detail: {"value": None, "candidate_index": None, "reason": detail}, persist=persist, transport=transport, now=now, ); d = inv.decision; return ModelSelection( d.get("value"), d.get("candidate_index"), str(d.get("reason", "")), inv.model, inv.provider, inv.prompt_sha256, inv.request_sha256, inv.response_sha256, inv.prompt_tokens, inv.completion_tokens, inv.invocation_id, inv.replayed, inv.served_model, )
SEGMENT_THEME_PROMPT_VERSION = "segment-theme:v2"; SEGMENT_THEME_INSTRUCTIONS = ( "You judge whether each of an issuer's reportable segments belongs to a stated investment " "theme. You are given the issuer's ticker and id, the theme, the definition of what counts " "as in-theme, and the segments as the issuer's own filing names and describes them. Judge " "that issuer and no other: never substitute a different company for it. Judge each segment " "on the theme definition alone. Answer true when the segment's revenue is predominantly in " "the theme, false when it predominantly is not, and null when the segment's name and " "description do not carry enough information to decide — null is a correct answer and is " "preferred over a guess. Return one verdict per segment, using the [index] given. Respond " 'ONLY with a JSON object of exactly this shape: {"verdicts": [{"index": <int>, ' '"in_theme": <true|false|null>, "reason": <one short sentence>}]}' ); SEGMENT_THEME_SCHEMA: dict[str, Any] = { "type": "object", "properties": { "verdicts": { "type": "array", "items": { "type": "object", "properties": { "index": {"type": "integer"}, "in_theme": {"type": ["boolean", "null"]}, "reason": {"type": "string"}, }, "required": ["index", "in_theme", "reason"], }, } }, "required": ["verdicts"], }; SEGMENT_THEME_TASK = ModelTask( SEGMENT_THEME_PROMPT_VERSION, SEGMENT_THEME_INSTRUCTIONS, SEGMENT_THEME_SCHEMA, max_tokens=900 )
@dataclass(frozen=True)
class ThemeClassification:
    verdicts: tuple[bool | None, ...]; reasons: tuple[str, ...]; invocation: ModelInvocation
    @property
    def extractor(self) -> str: return self.invocation.extractor
def _parse_theme_verdicts(content: str, segments: Sequence[str]) -> dict[str, Any]:
    verdicts: list[bool | None] = [None] * len(segments); reasons: list[str] = [""] * len(segments)
    if (items := _verdict_items(content)) is None: return {"verdicts": verdicts, "reasons": [f"unparseable model answer: {content[:120]!r}"] * len(segments)}
    for item in items:
        if isinstance(item, dict) and isinstance(idx := item.get("index"), int) and 0 <= idx < len(segments): verdicts[idx] = val if isinstance(val := item.get("in_theme"), bool) else None; reasons[idx] = str(item.get("reason", ""))[:400]
    return {"verdicts": verdicts, "reasons": reasons}
def _verdict_items(content: str) -> list[Any] | None:
    try: parsed = json.loads(content)
    except json.JSONDecodeError: return None
    objects = parsed if isinstance(parsed, list) else [parsed]
    if not all(isinstance(entry, dict) for entry in objects): return None
    return [item for entry in objects if isinstance(found := entry.get("verdicts"), list) for item in found]
def classify_segments( connection: Any | None, *, cik: int, accession: str, issuer_label: str, theme: str, inclusion: str, segments: Sequence[str], caller: str, standard: str = "segment_revenue", persist: bool = True, transport: Transport | None = None, now: Callable[[], datetime] = lambda: datetime.now(UTC), ) -> ThemeClassification: user = f"Issuer: {issuer_label}.\nTheme: {theme}.\nIn-theme means: {inclusion}\nSegments:\n" + "\n".join( f"[{i}] {name}" for i, name in enumerate(segments) ); inv = invoke( connection, task=SEGMENT_THEME_TASK, cik=cik, accession=accession, user_content=user, caller=caller, standard=standard, parse=lambda content: _parse_theme_verdicts(content, segments), refusal=lambda detail: {"verdicts": [None] * len(segments), "reasons": [detail] * len(segments)}, persist=persist, transport=transport, now=now, ); raw, raw_reasons = inv.decision.get("verdicts") or [], inv.decision.get("reasons") or []; verdicts = tuple((raw[i] if i < len(raw) and isinstance(raw[i], bool) else None) for i in range(len(segments))); reasons = tuple(str(raw_reasons[i]) if i < len(raw_reasons) else "" for i in range(len(segments))); return ThemeClassification(verdicts=verdicts, reasons=reasons, invocation=inv)
def _jsonb_or_null(body: bytes) -> str | None:
    try: return json.dumps(json.loads(body.decode()), ensure_ascii=False)
    except (UnicodeDecodeError, json.JSONDecodeError): return None
def _parse_decision(content: str, candidates: Sequence[Candidate]) -> dict[str, Any]:
    try: parsed = json.loads(content)
    except json.JSONDecodeError: parsed = None
    if not isinstance(parsed, dict): return {"value": None, "candidate_index": None, "reason": f"unparseable model answer: {content[:120]!r}"}
    value, index, reason = parsed.get("value"), parsed.get("candidate_index"), str(parsed.get("reason", ""))[:400]
    if value is None: return {"value": None, "candidate_index": None, "reason": reason or "model answered null"}
    try: value = int(value)
    except (TypeError, ValueError): return {"value": None, "candidate_index": None, "reason": f"non-integer model value {value!r}"}
    if not (matching := [i for i, c in enumerate(candidates) if c.value == value]): return {"value": None, "candidate_index": None, "reason": f"model chose {value}, which is not a candidate"}
    chosen = index if isinstance(index, int) and index in matching else matching[0]; return {"value": value, "candidate_index": chosen, "reason": reason}
def as_selection(selection: ModelSelection) -> extraction_primitive.Selection | None:
    if selection.value is None or selection.candidate_index is None: return None
    return extraction_primitive.Selection( selection.value, selection.candidate_index, selection.extractor, selection.reason, selection.invocation_id )
def _integral_value(value: int | float) -> int:
    if isinstance(value, int) or value.is_integer(): return int(value)
    raise ValueError(f"as_selector() received a non-integral candidate value {value!r}; headcount candidates are int")
def as_selector( connection: Any | None, *, cik: int, accession: str, form: str, issuer_label: str, caller: str, standard: str = "employees_total", persist: bool = True, transport: Transport | None = None, now: Callable[[], datetime] = lambda: datetime.now(UTC), on_model_selection: Callable[[ModelSelection], None] | None = None, ) -> extraction_primitive.Selector:
    def select(candidates: Sequence[extraction_primitive.Candidate]) -> extraction_primitive.Selection | None:
        selection = select_headcount( connection, cik=cik, accession=accession, form=form, issuer_label=issuer_label, candidates=[Candidate(value=_integral_value(c.value), sentence=c.sentence) for c in candidates], caller=caller, standard=standard, persist=persist, transport=transport, now=now, )
        if on_model_selection is not None: on_model_selection(selection)
        return as_selection(selection)
    return select
SUPPLY_CHAIN_PROMPT_VERSION = "supply-chain-extract:v1"; SUPPLY_CHAIN_INSTRUCTIONS = ( "You extract explicitly named external supply-chain partner organizations (suppliers and " "customers) from an issuer's annual SEC 10-K filing text. Rules: " "1. Extract ONLY specific, named third-party companies or corporate groups (e.g., 'Taiwan " "Semiconductor Manufacturing Company Limited', 'SK Hynix Inc.', 'McKesson Corporation'). " "2. Never extract generic groups ('third parties', 'various suppliers', 'cloud providers', " "'retail customers', 'government agencies'), internal segments, subsidiaries of the issuer, " "auditors, or executive biographies. " "3. Set relation_type to 'supplier' when the partner manufactures, fabricates, supplies " "components/materials/foundry services to, or is a subcontractor for the issuer. " "4. Set relation_type to 'customer' when the partner buys from, distributes for, or accounts " "for revenue of the issuer. " "5. Provide the canonical corporate name in partner_name, optional ticker if known or null, " "optional revenue_share_pct (0 to 100 numeric) if explicitly stated or null, and a verbatim " "evidence_quote (<=240 chars) from the filing text. Deduplicate partners by canonical company " "and relation_type. " 'Respond ONLY with a JSON object of this shape: {"partners": [{"partner_name": <str>, ' '"ticker": <str|null>, "relation_type": <"supplier"|"customer">, ' '"revenue_share_pct": <number|null>, "evidence_quote": <str>}]}' ); SUPPLY_CHAIN_SCHEMA: dict[str, Any] = { "type": "object", "properties": { "partners": { "type": "array", "items": { "type": "object", "properties": { "partner_name": {"type": "string"}, "ticker": {"type": ["string", "null"]}, "relation_type": {"type": "string", "enum": ["supplier", "customer"]}, "revenue_share_pct": {"type": ["number", "null"]}, "evidence_quote": {"type": "string"}, }, "required": ["partner_name", "relation_type", "evidence_quote"], }, } }, "required": ["partners"], }; SUPPLY_CHAIN_TASK = ModelTask( SUPPLY_CHAIN_PROMPT_VERSION, SUPPLY_CHAIN_INSTRUCTIONS, SUPPLY_CHAIN_SCHEMA, max_tokens=2400, timeout_seconds=180, max_context_tokens=MAX_CONTEXT_TOKENS, )
@dataclass(frozen=True)
class ExtractedSupplyChainPartner: partner_name: str; ticker: str | None; relation_type: str; revenue_share_pct: Decimal | None; evidence_quote: str
@dataclass(frozen=True)
class SupplyChainExtraction:
    partners: tuple[ExtractedSupplyChainPartner, ...]; invocation: ModelInvocation
    @property
    def extractor(self) -> str: return self.invocation.extractor
def _normalize_partner_entry(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict): return None
    name = str(item.get("partner_name") or "").strip(); rel = str(item.get("relation_type") or "").strip().lower(); quote = str(item.get("evidence_quote") or "").strip()[:240]
    if not name or rel not in ("supplier", "customer") or not quote: return None
    raw_ticker, raw_share, share_str = item.get("ticker"), item.get("revenue_share_pct"), None; ticker = str(raw_ticker).strip().upper() if isinstance(raw_ticker, str) and raw_ticker.strip() else None
    if isinstance(raw_share, int | float | str) and not isinstance(raw_share, bool):
        try:
            if Decimal("0") <= (val := Decimal(str(raw_share))) <= Decimal("100"): share_str = str(val)
        except Exception: share_str = None
    return { "partner_name": name, "ticker": ticker, "relation_type": rel, "revenue_share_pct": share_str, "evidence_quote": quote, }
def _parse_supply_chain_partners(content: str) -> dict[str, Any]:
    try: parsed = json.loads(content)
    except json.JSONDecodeError: return {"partners": [], "reason": f"unparseable model answer: {content[:120]!r}"}
    objects = parsed if isinstance(parsed, list) else [parsed]
    if not all(isinstance(entry, dict) for entry in objects): return {"partners": [], "reason": f"unparseable model answer: {content[:120]!r}"}
    partners: list[dict[str, Any]] = []; seen: set[tuple[str, str]] = set()
    for entry in objects:
        if isinstance(found := entry.get("partners"), list):
            for item in found:
                if (norm := _normalize_partner_entry(item)) is not None:
                    if (key := (norm["relation_type"], norm["partner_name"].lower())) not in seen: seen.add(key); partners.append(norm)
    return {"partners": partners, "reason": "ok"}
_SUPPLY_CHAIN_MAX_FILING_CHARS = (MAX_CONTEXT_TOKENS - 8_000) // 8 - 4_000; _SUPPLY_CHAIN_ANCHOR_RE = re.compile( r"supply[\s-]*chain|customer|supplier|concentration|segment|10%|item\s+1a?\b", re.IGNORECASE )
def _merge_anchor_spans(text: str, start_bound: int, end_bound: int, budget: int) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []; used = 0
    for match in _SUPPLY_CHAIN_ANCHOR_RE.finditer(text, start_bound, end_bound):
        win_start, win_end = max(start_bound, match.start() - 1_500), min(end_bound, match.end() + 2_500)
        if spans and win_start <= spans[-1][1]:
            prev_s, prev_e = spans[-1]
            if used + (added := max(0, win_end - prev_e)) <= budget: spans[-1], used = (prev_s, max(prev_e, win_end)), used + added
            continue
        if used + (win_end - win_start) > budget: break
        spans.append((win_start, win_end)); used += win_end - win_start
    return spans
def _bound_supply_chain_filing_text(filing_text: str) -> str:
    if ( len(filing_text) <= _SUPPLY_CHAIN_MAX_FILING_CHARS or int(len(filing_text) / _CHARS_PER_TOKEN_FLOOR) > MAX_CONTEXT_TOKENS ): return filing_text
    head_len, tail_len = _SUPPLY_CHAIN_MAX_FILING_CHARS // 3, _SUPPLY_CHAIN_MAX_FILING_CHARS // 6; mid_budget, tail_start = ( _SUPPLY_CHAIN_MAX_FILING_CHARS - head_len - tail_len - 128, len(filing_text) - (_SUPPLY_CHAIN_MAX_FILING_CHARS // 6), ); spans = _merge_anchor_spans(filing_text, head_len, tail_start, mid_budget); return "\n...\n".join([filing_text[:head_len], *(filing_text[s:e] for s, e in spans), filing_text[tail_start:]])
def identify_supply_chain_partners( connection: Any | None, *, cik: int, accession: str, issuer_label: str, filing_text: str, caller: str = "supply_chain_extraction", standard: str = "supply_chain", persist: bool = True, transport: Transport | None = None, now: Callable[[], datetime] = lambda: datetime.now(UTC), ) -> SupplyChainExtraction: bounded_text = _bound_supply_chain_filing_text(filing_text); user = f"Issuer: {issuer_label} (CIK {cik:010d}), form 10-K, accession {accession}.\nFiling text:\n{bounded_text}"; inv = invoke( connection, task=SUPPLY_CHAIN_TASK, cik=cik, accession=accession, user_content=user, caller=caller, standard=standard, parse=_parse_supply_chain_partners, refusal=lambda detail: {"partners": [], "reason": detail}, persist=persist, transport=transport, now=now, ); partners = [ ExtractedSupplyChainPartner( norm["partner_name"], norm["ticker"], norm["relation_type"], Decimal(norm["revenue_share_pct"]) if norm["revenue_share_pct"] is not None else None, norm["evidence_quote"], ) for item in (inv.decision.get("partners") or []) if (norm := _normalize_partner_entry(item)) is not None ]; return SupplyChainExtraction(partners=tuple(partners), invocation=inv)
XHS_CARD_RESPONSE_SCHEMA: dict[str, Any] = { "type": "object", "properties": {"html": {"type": "string"}}, "required": ["html"], }; XHS_CARD_TASK = ModelTask( XHS_CARD_PROMPT_VERSION, f'{XHS_CARD_INSTRUCTIONS}\nRespond ONLY with a JSON object of shape `{{"html": "<!DOCTYPE html>...</html>"}}`.', XHS_CARD_RESPONSE_SCHEMA, max_tokens=8_192, timeout_seconds=90, )
@dataclass(frozen=True)
class XhsCardGenerationResult: card_id: str; html: str; render_mode: str; prompt_sha256: str; model: str | None; invocation_id: str | None; replayed: bool
def _parse_xhs_card_html(content: str) -> dict[str, Any]:
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict) and isinstance(parsed.get("html"), str): return {"html": extract_html_document(parsed["html"]), "reason": "ok"}
    except json.JSONDecodeError: pass
    extracted = extract_html_document(content)
    if "<html" in extracted.lower() and "</html>" in extracted.lower(): return {"html": extracted, "reason": "ok"}
    return {"html": "", "reason": f"unparseable html output: {content[:120]!r}"}
def generate_xhs_card_html( connection: Any | None, *, card: ResearchCard, caller: str = "xhs_card_html", standard: str = "xhs_card", persist: bool = True, transport: Transport | None = None, now: Callable[[], datetime] = lambda: datetime.now(UTC), ) -> XhsCardGenerationResult:
    prompt_digest = XHS_CARD_TASK.prompt_sha256
    try:
        inv = invoke( connection, task=XHS_CARD_TASK, cik=(int(hashlib.sha256(card.card_id.encode()).hexdigest()[:8], 16) & 0x7FFFFFFF) or 1, accession=card.card_id[:36], user_content=build_card_prompt(card), caller=caller, standard=standard, parse=_parse_xhs_card_html, refusal=lambda detail: {"html": "", "reason": detail}, persist=persist, transport=transport, now=now, ); candidate_html = str(inv.decision.get("html") or ""); ok, violations = verify_card_html(card, candidate_html)
        if ok: return XhsCardGenerationResult( card.card_id, candidate_html, "model_glm", inv.prompt_sha256, inv.served_model or inv.model, inv.invocation_id, inv.replayed, )
        logger.warning( "xhs_card_html model output failed fact-preservation verifier for %s (%s); using deterministic fallback", card.card_id, violations, )
    except (ModelNotConfigured, ModelHTTPError) as exc: logger.info("xhs_card_html falling back to deterministic Cold-Verdict HTML for %s: %s", card.card_id, exc)
    return XhsCardGenerationResult( card.card_id, render_card_html(card), "deterministic_fallback", prompt_digest, None, None, False )
