"""Golden decision corpus models for strategy contract tests (#1716)."""

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BeforeValidator, Field, model_validator
from truealpha_contracts import common as _c
from truealpha_contracts.research import ValuationTier
from truealpha_contracts.strategy import ExclusionReason, GoldenDecisionOutcome, _dec
from truealpha_contracts.universe import SubjectKind, SubjectRef

_PAT, _EXCL = r"^[a-z][a-z0-9]*(?:[._:/-][a-z0-9]+)*$", GoldenDecisionOutcome.EXCLUDED
_DecP = Annotated[Decimal, BeforeValidator(_dec)]


def _check_imm(v: str) -> str:
    if {t for t in v.lower().replace("-", ".").split(".") if t} & _c.DEFAULT_MUTABLE_TOKENS:
        raise ValueError("mutable ref")
    return v


def _need(ok: bool, msg: str) -> None:
    if not ok:
        raise ValueError(msg)


class CutoffRiskFreeRate(_c.StrictFrozenModel):
    cutoff_at: _c.AwareDatetime
    annualized_rate: _DecP = Field(ge=0, le=1)
    provisional: Literal[True]


class GoldenInputRecord(_c.StrictFrozenModel):
    input_key: str = Field(pattern=_PAT)
    value: _DecP
    unit_code: str = Field(pattern=_PAT)
    confidence: _DecP = Field(gt=0, le=1)
    knowable_at: _c.AwareDatetime
    grounding: str = Field(min_length=1)


class GoldenExpectation(_c.OmitNoneFields, _c.StrictFrozenModel):
    omit_none_fields = ("current_price_to_gross_profit", "target_price_to_gross_profit")
    capital_adjusted_labor_efficiency: _DecP | None
    tier: ValuationTier | None
    current_price_to_sales: _DecP | None
    target_price_to_sales: _DecP | None
    valuation_gap: _DecP | None
    eligible: bool
    outcome: GoldenDecisionOutcome
    exclusion_reason: ExclusionReason | None
    rank: int | None = Field(ge=1)
    target_weight: _DecP | None = Field(ge=0, le=1)
    current_price_to_gross_profit: _DecP | None = None
    target_price_to_gross_profit: _DecP | None = None

    @model_validator(mode="after")
    def validate_outcome_consistency(self) -> Self:
        pgp = (self.current_price_to_gross_profit, self.target_price_to_gross_profit)
        if self.outcome is _EXCL:
            ok = self.exclusion_reason is not None and not self.eligible and self.rank is None
            _need(ok and self.target_weight is None and all(v is None for v in pgp), "invalid excluded")
            return self
        req = (self.capital_adjusted_labor_efficiency, self.tier, self.current_price_to_sales, self.valuation_gap)
        _need(self.exclusion_reason is None and self.eligible and all(v is not None for v in req), "invalid active")
        has_ps, has_pgp = self.target_price_to_sales is not None, all(v is not None for v in pgp)
        _need((has_ps and all(v is None for v in pgp)) or (not has_ps and has_pgp), "exact one basis")
        rk = {
            GoldenDecisionOutcome.SELECTED: (1, 1),
            GoldenDecisionOutcome.RANKED_BEYOND_SELECTION_COUNT: (1, 0),
            GoldenDecisionOutcome.REJECTED_VALUATION_ABOVE_TIER_BAND: (0, 0),
        }
        _need((int(self.rank is not None), int(self.target_weight is not None)) == rk[self.outcome], "invalid rank")
        return self


class GoldenDecision(_c.StrictFrozenModel):
    decision_key: Annotated[str, AfterValidator(_check_imm)] = Field(pattern=_PAT)
    issuer: SubjectRef
    issuer_branch: Literal["non_financial", "financial"]
    cutoff_at: _c.AwareDatetime
    inputs: tuple[GoldenInputRecord, ...] = Field(min_length=1)
    derivation: str = Field(min_length=1)
    expected: GoldenExpectation

    @model_validator(mode="after")
    def validate_and_canonicalize(self) -> Self:
        inp = tuple(sorted(self.inputs, key=lambda i: i.input_key))
        ok = self.issuer.kind is SubjectKind.ISSUER and len({i.input_key for i in inp}) == len(inp)
        _need(ok and all(r.knowable_at <= self.cutoff_at for r in inp), "invalid golden decision")
        object.__setattr__(self, "inputs", inp)
        return self


class GoldenDecisionSet(_c.StrictIdentifiedFrozenModel):
    id_field, id_prefix = "golden_decision_set_id", "golden-decision-set"
    golden_decision_set_id: str = _c.content_id("golden-decision-set")
    content_sha256: _c.ContentHash = ""
    strategy_definition_id: str = _c.content_ref("strategy-definition")
    strategy_definition_sha256: _c.Sha256
    risk_free_rates: tuple[CutoffRiskFreeRate, ...] = Field(min_length=2)
    decisions: tuple[GoldenDecision, ...] = Field(min_length=6)

    def _canonicalize(self) -> None:
        rates = tuple(sorted(self.risk_free_rates, key=lambda i: i.cutoff_at))
        decs = tuple(sorted(self.decisions, key=lambda i: (i.cutoff_at, i.issuer.id)))
        cutoffs = {d.cutoff_at for d in decs}
        req_out = {GoldenDecisionOutcome.SELECTED, GoldenDecisionOutcome.REJECTED_VALUATION_ABOVE_TIER_BAND, _EXCL}
        uniq = len({i.cutoff_at for i in rates}) == len(rates) and len({d.decision_key for d in decs}) == len(decs)
        ok_id = self.strategy_definition_id.endswith(f":{self.strategy_definition_sha256}")
        ok_k = uniq and len({(d.issuer.id, d.cutoff_at) for d in decs}) == len(decs)
        ok_c = cutoffs == {i.cutoff_at for i in rates} and len({d.issuer.id for d in decs}) >= 3 and len(cutoffs) >= 2
        _need(ok_id and ok_k and ok_c and req_out <= {d.expected.outcome for d in decs}, "invalid golden set")
        for c in cutoffs:
            ranked = sorted(
                (d for d in decs if d.cutoff_at == c and d.expected.rank is not None),
                key=lambda d: d.expected.rank or 0,
            )
            sel = [d for d in ranked if d.expected.outcome is GoldenDecisionOutcome.SELECTED]
            w = [d.expected.target_weight for d in sel if d.expected.target_weight is not None]
            ok_r = [d.expected.rank for d in ranked] == list(range(1, len(ranked) + 1)) and sel == ranked[: len(sel)]
            _need(ok_r and (not w or sum(w) <= Decimal(1)) and len(set(w)) <= 1, "invalid ranks or weights")
        object.__setattr__(self, "risk_free_rates", rates)
        object.__setattr__(self, "decisions", decs)
