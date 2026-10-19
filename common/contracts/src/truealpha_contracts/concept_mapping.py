"""Which vendor concepts a normalized field is drawn from, as data rather than code.

The concept lists behind revenue, cost of revenue, share count and the bank operating
numerator lived as Python tuples inside the SEC adapter. Every correction — a newly
observed tag, a variant an issuer switched to — therefore required a code change and a
deploy, on a plane that moves far more often than the pipeline itself.

This contract is that declaration as a content-addressed object. Publishing a new ruleset
is an insert into `staging.contract_objects` plus a pointer advance; the running image is
untouched, and every observation records the ruleset hash that produced it, so a number
stays explainable after the rules move on.

## The distinction the ruleset is really carrying

`ResolutionKind` is the load-bearing part, not the concept lists.

SYNONYM variants are the same quantity tagged differently at different times — `Revenues`
and `RevenueFromContractWithCustomerExcludingAssessedTax` are both total revenue, the
second replaced the first at ASC 606. They merge into one period-keyed series and the
latest period wins, which is what follows an issuer across the switch.

FALLBACK variants are *different quantities*, ordered by how well each stands in. A more
recent number for a different quantity is still a different quantity: `CommonStockSharesIssued`
includes treasury stock (JNJ: 3,119,843,000 issued against 2,409,898,597 outstanding), and
plain `Revenues` for a bank is gross of interest expense. These resolve to the first
concept the issuer reports at all, so a stand-in is reached only when the exact quantity is
absent — never because it carries a later date.

Collapsing the two is how a 29%-wrong share count gets in without anything looking wrong,
so the kind is declared per field and cannot be defaulted.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from truealpha_contracts.common import canonical_sha256


class ResolutionKind(StrEnum):
    SYNONYM = "synonym"
    FALLBACK = "fallback"


class ConceptRef(BaseModel):
    """One vendor concept: its taxonomy and name. `dei` and `us-gaap` are both real
    homes for facts we need, so the taxonomy is never assumed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    taxonomy: str = Field(min_length=1)
    concept: str = Field(min_length=1)


class FieldMapping(BaseModel):
    """How one normalized field resolves out of vendor concepts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    field: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    kind: ResolutionKind
    concepts: tuple[ConceptRef, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique(self) -> Self:
        seen = [(item.taxonomy, item.concept) for item in self.concepts]
        if len(set(seen)) != len(seen):
            raise ValueError(f"{self.field}: concept list repeats an entry")
        return self


class ConceptMappingRuleset(BaseModel):
    """A complete, content-addressed concept mapping.

    `ruleset_id` is derived from the content, so two publishers who declare the same rules
    land the same identity and a changed rule is a different object by construction — the
    property that lets `mapping_version` name exactly which rules produced a figure.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ruleset_id: str = Field(default="", pattern=r"^(?:|concept-mapping:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    version: str = Field(min_length=1)
    mappings: tuple[FieldMapping, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def identify(self) -> Self:
        fields = [item.field for item in self.mappings]
        if len(set(fields)) != len(fields):
            raise ValueError("ruleset declares the same field twice")
        payload = self.model_dump(mode="json", exclude={"ruleset_id", "content_sha256"})
        digest = canonical_sha256(payload)
        expected = f"concept-mapping:{digest}"
        if self.ruleset_id not in {"", expected} or self.content_sha256 not in {"", digest}:
            raise ValueError("concept mapping identity does not match its canonical content")
        object.__setattr__(self, "ruleset_id", expected)
        object.__setattr__(self, "content_sha256", digest)
        return self

    def mapping_for(self, field: str) -> FieldMapping | None:
        for item in self.mappings:
            if item.field == field:
                return item
        return None
