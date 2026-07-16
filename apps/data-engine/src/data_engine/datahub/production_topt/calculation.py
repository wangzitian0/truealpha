"""Production GPPE calculation with explicit denominator gaps."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import Any

from factors.batches.issuer_tier_valuation_tiny import tier_for_gppe
from factors.batches.issuer_tier_valuation_tiny.kernel import ValuationTier
from psycopg import Connection
from psycopg.types.json import Jsonb
from truealpha_contracts import canonical_sha256

_TARGET_PS = {
    ValuationTier.TRADITIONAL: (Decimal("3"), Decimal("4")),
    ValuationTier.TECH: (Decimal("8"), Decimal("10")),
    ValuationTier.LARGE_MODEL_NATIVE: (Decimal("20"), Decimal("30")),
}


class GppeCalculationAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class GppeCalculationInput:
    run_id: str
    issuer_id: str
    cutoff: datetime
    gross_profit: Decimal | None
    employee_headcount: int | None
    is_financial: bool
    confidence: Decimal


@dataclass(frozen=True)
class GppeCalculationResult:
    result_id: str
    content_sha256: str
    run_id: str
    issuer_id: str
    cutoff: datetime
    availability: GppeCalculationAvailability
    gross_profit: Decimal | None
    employee_headcount: int | None
    gppe: Decimal | None
    tier: ValuationTier | None
    target_ps_lower: Decimal | None
    target_ps_upper: Decimal | None
    confidence: Decimal
    reason_codes: tuple[str, ...]


class PostgresGppeResultRepository:
    def __init__(self, connection: Connection[Any]) -> None:
        self._connection = connection

    def put(self, result: GppeCalculationResult) -> bool:
        payload = {
            "result_id": result.result_id,
            "content_sha256": result.content_sha256,
            "run_id": result.run_id,
            "issuer_id": result.issuer_id,
            "cutoff": result.cutoff.isoformat(),
            "availability": result.availability.value,
            "gross_profit": str(result.gross_profit) if result.gross_profit is not None else None,
            "employee_headcount": result.employee_headcount,
            "gppe": str(result.gppe) if result.gppe is not None else None,
            "tier": result.tier.value if result.tier is not None else None,
            "target_ps_lower": str(result.target_ps_lower) if result.target_ps_lower is not None else None,
            "target_ps_upper": str(result.target_ps_upper) if result.target_ps_upper is not None else None,
            "confidence": str(result.confidence),
            "reason_codes": result.reason_codes,
        }
        inserted = self._connection.execute(
            """
            insert into mart.topt_gppe_results (
                result_id, content_sha256, run_id, issuer_id, cutoff, availability,
                gross_profit, employee_headcount, gppe, tier, target_ps_lower,
                target_ps_upper, confidence, reason_codes, payload
            ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (result_id) do nothing returning result_id
            """,
            (
                result.result_id,
                result.content_sha256,
                result.run_id,
                result.issuer_id,
                result.cutoff,
                result.availability.value,
                result.gross_profit,
                result.employee_headcount,
                result.gppe,
                result.tier.value if result.tier is not None else None,
                result.target_ps_lower,
                result.target_ps_upper,
                result.confidence,
                list(result.reason_codes),
                Jsonb(payload),
            ),
        ).fetchone()
        if inserted is not None:
            return True
        existing = self._connection.execute(
            "select content_sha256 from mart.topt_gppe_results where result_id = %s",
            (result.result_id,),
        ).fetchone()
        if existing is None or existing[0] != result.content_sha256:
            raise ValueError("GPPE result identity is bound to different content")
        return False

    def for_run(self, run_id: str, *, limit: int = 100, offset: int = 0) -> tuple[dict[str, Any], ...]:
        if not 1 <= limit <= 500 or offset < 0:
            raise ValueError("GPPE pagination is outside the bounded range")
        rows = self._connection.execute(
            """
            select payload from mart.topt_gppe_results
            where run_id = %s order by issuer_id limit %s offset %s
            """,
            (run_id, limit, offset),
        ).fetchall()
        return tuple(row[0] for row in rows)


def calculate_gppe(value: GppeCalculationInput) -> GppeCalculationResult:
    if value.cutoff.tzinfo is None or value.cutoff.utcoffset() is None:
        raise ValueError("GPPE cutoff must be timezone-aware")
    if not Decimal("0") <= value.confidence <= Decimal("1"):
        raise ValueError("GPPE confidence must be between zero and one")

    reasons: tuple[str, ...] = ()
    gppe: Decimal | None = None
    tier: ValuationTier | None = None
    target_low: Decimal | None = None
    target_high: Decimal | None = None
    if value.is_financial:
        reasons = ("financial_gppe_mapping_unapproved",)
    elif value.gross_profit is None:
        reasons = ("missing_gross_profit",)
    elif value.employee_headcount is None:
        reasons = ("missing_employee_headcount",)
    elif value.employee_headcount <= 0:
        reasons = ("nonpositive_employee_headcount",)
    else:
        with localcontext() as context:
            context.prec = 34
            gppe = value.gross_profit / Decimal(value.employee_headcount)
        tier = tier_for_gppe(gppe)
        target_low, target_high = _TARGET_PS[tier]

    availability = (
        GppeCalculationAvailability.AVAILABLE if gppe is not None else GppeCalculationAvailability.UNAVAILABLE
    )
    confidence = value.confidence if gppe is not None else Decimal("0")
    hash_payload = {
        "run_id": value.run_id,
        "issuer_id": value.issuer_id,
        "cutoff": value.cutoff.isoformat(),
        "availability": availability.value,
        "gross_profit": str(value.gross_profit) if value.gross_profit is not None else None,
        "employee_headcount": value.employee_headcount,
        "gppe": str(gppe) if gppe is not None else None,
        "tier": tier.value if tier is not None else None,
        "target_ps_lower": str(target_low) if target_low is not None else None,
        "target_ps_upper": str(target_high) if target_high is not None else None,
        "confidence": str(confidence),
        "reason_codes": reasons,
    }
    digest = canonical_sha256(hash_payload)
    return GppeCalculationResult(
        result_id=f"topt-gppe-result:{digest}",
        content_sha256=digest,
        run_id=value.run_id,
        issuer_id=value.issuer_id,
        cutoff=value.cutoff,
        availability=availability,
        gross_profit=value.gross_profit,
        employee_headcount=value.employee_headcount,
        gppe=gppe,
        tier=tier,
        target_ps_lower=target_low,
        target_ps_upper=target_high,
        confidence=confidence,
        reason_codes=reasons,
    )
