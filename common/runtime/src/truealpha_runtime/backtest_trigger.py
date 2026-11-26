"""Shared governed backtest trigger contract for API, MCP, and CLI (`init.md` §0.2, Q05)."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from truealpha_contracts.strategy import LargeModelValueV0Definition, load_strategy_definition

_SAFE_RESOURCE_RE = re.compile(r"^[A-Za-z0-9_.-]+\.json$")
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:@-]+$")


def parse_utc_timestamp(raw: str, label: str) -> datetime:
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid {label} timestamp") from exc
    return (dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)).astimezone(UTC)


@dataclass(frozen=True)
class PreparedBacktestTrigger:
    executed_at: datetime
    executed_iso: str
    dedupe_key: str
    run_key: str
    config: dict[str, Any]
    config_json: str
    custom_definition_sha256: str | None
    release_manifest_id: str | None
    exploratory: bool


def prepare_backtest_trigger(
    *,
    definition_resource: str | None = None,
    custom_definition: dict[str, Any] | None = None,
    release_manifest_id: str | None = None,
    start_cutoff: str | None = None,
    end_cutoff: str | None = None,
    executed_at: str | None = None,
    idempotency_key: str | None = None,
) -> PreparedBacktestTrigger:
    if definition_resource and custom_definition is not None:
        raise ValueError("specify either definition_resource or custom_definition, not both")
    config: dict[str, Any] = {}
    if definition_resource:
        if not _SAFE_RESOURCE_RE.match(definition_resource) or ".." in definition_resource:
            raise ValueError("invalid strategy definition resource name")
        try:
            load_strategy_definition(definition_resource)
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise ValueError(f"unknown strategy definition: {definition_resource}") from exc
        config["definition_resource"] = definition_resource

    custom_sha: str | None = None
    if custom_definition is not None:
        try:
            validated = LargeModelValueV0Definition.model_validate_json(json.dumps(custom_definition))
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"invalid custom strategy definition: {exc}") from exc
        custom_sha = validated.content_sha256
        config["custom_definition"] = json.loads(validated.model_dump_json())
        config["custom_definition_sha256"] = custom_sha

    clean_manifest = release_manifest_id.strip() if release_manifest_id else None
    if release_manifest_id is not None and not clean_manifest:
        raise ValueError("release_manifest_id must be non-empty when provided")
    if clean_manifest is not None:
        config["release_manifest_id"] = clean_manifest

    executed_at_dt = (
        parse_utc_timestamp(executed_at, "executed_at") if executed_at else datetime.now(UTC).replace(microsecond=0)
    )
    executed_iso = executed_at_dt.isoformat()
    start_dt = parse_utc_timestamp(start_cutoff, "start_cutoff") if start_cutoff else None
    end_dt = parse_utc_timestamp(end_cutoff, "end_cutoff") if end_cutoff else None
    if start_dt is not None and end_dt is not None and start_dt > end_dt:
        raise ValueError("start_cutoff must not exceed end_cutoff")
    if start_dt is not None:
        config["start_cutoff"] = start_dt.isoformat()
    if end_dt is not None:
        config["end_cutoff"] = end_dt.isoformat()

    exploratory = bool(custom_definition is not None or start_dt is not None or end_dt is not None)
    if exploratory:
        config["exploratory"] = True

    raw_idem = (idempotency_key or "").strip()
    if idempotency_key is not None and raw_idem and not _IDEMPOTENCY_KEY_RE.fullmatch(raw_idem):
        raise ValueError("invalid idempotency_key format")
    dedupe_key = raw_idem if raw_idem else str(uuid.uuid4())
    return PreparedBacktestTrigger(
        executed_at=executed_at_dt,
        executed_iso=executed_iso,
        dedupe_key=dedupe_key,
        run_key=f"manual:{dedupe_key}",
        config=config,
        config_json=json.dumps(config, sort_keys=True),
        custom_definition_sha256=custom_sha,
        release_manifest_id=clean_manifest,
        exploratory=exploratory,
    )


def enqueue_backtest_trigger(cur: Any, *, principal_id: str, prepared: PreparedBacktestTrigger) -> int:
    cur.execute(
        """
        with inserted as (
            insert into staging.pipeline_trigger_requests (
                job_name, executed_at, requested_by, dedupe_key, force_fetch, config_json
            )
            values ('nightly_backtest_pipeline', %s, %s, %s, false, %s::jsonb)
            on conflict (dedupe_key) do nothing
            returning request_id
        )
        select request_id from inserted
        union all
        select request_id from staging.pipeline_trigger_requests where dedupe_key = %s
        limit 1
        """,
        (prepared.executed_at, principal_id, prepared.dedupe_key, prepared.config_json, prepared.dedupe_key),
    )
    row = cur.fetchone()
    if isinstance(row, dict):
        return int(row["request_id"])
    return int(row[0])
