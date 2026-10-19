"""Module 6: the theme-purity definition (#772, init.md §0 question 6).

init.md §0 question 6 asks who the purest name under a given theme is. "Given" is the
load-bearing word: a theme is an INPUT, and an input that changes between two runs makes
their answers incomparable while both look like percentages. So a theme is declared here,
content-addressed, and a run records which version it answered under — the same contract
`EtfConsolidationDefinition` holds for module 5.

What a definition has to state, because each of these silently moves every share:

- `theme` and `inclusion` — the words a classifier is judged against. A theme named but
  not defined ("AI") is a different question to every reader and to every model revision;
  the inclusion text is the actual question asked, so it is part of the identity.
- `minimum_classified_share` — the refusal floor. A purity of 0.6 computed while 45% of
  the issuer's revenue was never classified is not a purity, it is a guess with a decimal
  point. Below the floor the row is refused rather than published, exactly as #36 refuses
  a thinly-covered fund instead of presenting a confident-looking aggregate.

What a definition deliberately does NOT state: which segments are in the theme. That is
the judgement (`factors.base.theme_purity` consumes it and never makes it), and writing an
issuer's segments into a governed constant would turn a measurement into a list someone
maintains by hand — the defect shape AGENTS.md names first.
"""

from __future__ import annotations

from decimal import Decimal
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from truealpha_contracts.common import canonical_sha256


class ThemeDefinition(BaseModel):
    """A versioned, content-addressed statement of one theme and how membership is judged."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    factor_key: Literal["theme_purity"] = "theme_purity"
    factor_version: str = Field(pattern=r"^v[0-9]+$")
    #: Stable id, used as the mart row's key and in the classifier's prompt. Kebab-case so
    #: it survives a URL, a column value and a log line unchanged.
    theme_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    #: The human name, as a page would print it.
    theme: str = Field(min_length=1)
    #: The question actually asked of the classifier. Part of the identity because two
    #: runs under different wording are two different questions, not two measurements.
    inclusion: str = Field(min_length=1)
    #: Below this share of consolidated revenue classified either way, the row is refused.
    #: A fraction (0-1), not a percentage — the same unit `theme_share` is in.
    minimum_classified_share: Decimal = Field(ge=0, le=1)

    @property
    def content_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


#: v0 themes. Three, not thirty: each one costs a classifier call per segment per issuer,
#: and a theme nobody reads is spend with no reader. They are the ones init.md §0 and
#: vision.md actually name.
#:
#: `minimum_classified_share` is 0.80 rather than 1.0 because a partition can be accepted
#: with a small residual and a classifier is entitled to decline on a genuinely ambiguous
#: segment — refusing every issuer with one unclear line would publish nothing. It is not
#: 0.5: a share whose denominator is half unexamined ranks its own coverage.
THEMES: MappingProxyType[str, ThemeDefinition] = MappingProxyType(
    {
        theme.theme_id: theme
        for theme in (
            ThemeDefinition(
                factor_version="v0",
                theme_id="ai-infrastructure",
                theme="AI infrastructure",
                inclusion=(
                    "Revenue from hardware, software or services whose primary purpose is training "
                    "or serving machine-learning models: accelerators and the networking, memory and "
                    "power systems built for them, data-centre capacity sold for AI workloads, and "
                    "model training or inference sold as a service. General-purpose computing, "
                    "consumer devices, and software that merely embeds a model feature are NOT in "
                    "the theme."
                ),
                minimum_classified_share=Decimal("0.80"),
            ),
            ThemeDefinition(
                factor_version="v0",
                theme_id="cloud-software",
                theme="Cloud software",
                inclusion=(
                    "Revenue from software delivered and billed as a subscription service the vendor "
                    "operates, including the infrastructure and platform capacity sold to run it. "
                    "Perpetual licences, on-premise maintenance, hardware and professional services "
                    "are NOT in the theme."
                ),
                minimum_classified_share=Decimal("0.80"),
            ),
            ThemeDefinition(
                factor_version="v0",
                theme_id="semiconductors",
                theme="Semiconductors",
                inclusion=(
                    "Revenue from designing, manufacturing, packaging or testing integrated circuits, "
                    "and from the equipment and materials used to do so. Systems that merely contain "
                    "semiconductors, and software sold alongside them, are NOT in the theme."
                ),
                minimum_classified_share=Decimal("0.80"),
            ),
        )
    }
)
