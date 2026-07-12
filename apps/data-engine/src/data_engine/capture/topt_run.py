"""The single coordinator shared by TOPT CLI and Dagster execution."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from truealpha_contracts import CaptureManifest

from data_engine.capture import manifest as manifest_builder
from data_engine.capture import repository, source_results
from data_engine.capture.topt import build_topt_scope
from data_engine.capture.topt_identity import capture as capture_identity
from data_engine.capture.topt_identity import emit_source_results as emit_identity_results
from data_engine.capture.topt_sources import (
    capture_moomoo_domains,
    capture_sec_filings,
    capture_sec_financials,
    capture_yahoo_prices,
)


@dataclass(frozen=True)
class ToptRunOptions:
    image_digest: str
    run_id: str | None = None
    reuse_openfigi_raw: bool = False
    identity: bool = True
    sec_financials: bool = True
    sec_filings: bool = True
    yahoo_prices: bool = True
    moomoo_domains: bool = True


def execute(conn, options: ToptRunOptions) -> CaptureManifest:
    started_at = datetime.now(UTC)
    run_id = options.run_id or f"topt:{started_at.strftime('%Y%m%dT%H%M%SZ')}:{uuid.uuid4().hex[:12]}"
    scope = build_topt_scope()
    repository.put_scope(conn, scope)
    conn.commit()

    if options.identity:
        identity = capture_identity(conn, reuse_openfigi_raw=options.reuse_openfigi_raw)
        emit_identity_results(conn, run_id=run_id, scope=scope, result=identity)
        conn.commit()
    if options.sec_financials:
        capture_sec_financials(conn, run_id=run_id, scope=scope)
    if options.sec_filings:
        capture_sec_filings(conn, run_id=run_id, scope=scope)
    if options.yahoo_prices:
        capture_yahoo_prices(conn, run_id=run_id, scope=scope)
    if options.moomoo_domains:
        capture_moomoo_domains(conn, run_id=run_id, scope=scope)

    for requirement in scope.requirements:
        if source_results.for_cell(conn, run_id, requirement):
            source_results.finalize(conn, run_id=run_id, requirement=requirement)
    completed_at = datetime.now(UTC)
    manifest = manifest_builder.build(
        conn,
        scope=scope,
        run_id=run_id,
        image_digest=options.image_digest,
        as_of=completed_at,
        started_at=started_at,
        completed_at=completed_at,
    )
    repository.put_manifest(conn, manifest)
    conn.commit()
    return manifest
