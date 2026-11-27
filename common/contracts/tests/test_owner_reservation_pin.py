"""Pins the owner's production reservation sentences in truealpha/AGENTS.md."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

PINS: dict[str, tuple[str, ...]] = {
    "AGENTS.md": (
        "## Production reservation",
        "The single definition of the production reservation.",
        "**Only a production change returns to the owner.** A production change alters what production runs or stores (deploy, promote or apply to production; production secret write; manual production data write or deletion; production DNS, route or host change; unrecallable release; or scheduled unapproved production code).",
        "**Self-guard:** an edit that removes, narrows or redefines the production reservation needs the owner's approval of its head SHA.",
        "It also covers removing or weakening a pin of this section, its pin test, or the required check that runs the test.",
        "Production here (`docs/release-protocol.md`): the infra2 workflow `reconcile-iac-inputs.yml` with `promote_prod=true` and the infra2 workflow `deploy.yml` with `type=prod`.",
        "The owner-approval checks of those infra2 paths are part of the reservation itself.",
    ),
}

_LINE_START = re.compile(r"(?:\A|\n)[ \t]*(?:(?:[->+]|\d+\.)[ \t]+)*\Z")
_AFTER_SENTENCE_END = re.compile(r"\.\s+\Z")


def _pattern(sentence: str) -> re.Pattern[str]:
    return re.compile(r"\s+".join(re.escape(word) for word in sentence.split()))


def starts_a_sentence(text: str, sentence: str) -> bool:
    """Return True when ``sentence`` appears at the start of a line or after a sentence break."""
    for match in _pattern(sentence).finditer(text):
        before = text[: match.start()]
        if _LINE_START.search(before) or _AFTER_SENTENCE_END.search(before):
            return True
    return False


def test_owner_reservation_sentences_are_pinned_in_agents_md() -> None:
    """Reject removing, negating, or weakening production-reservation pins in AGENTS.md."""
    for rel, sentences in PINS.items():
        text = (ROOT / rel).read_text(encoding="utf-8")
        for sentence in sentences:
            assert starts_a_sentence(text, sentence), (
                f"{rel}: missing or modified production reservation pin: {sentence!r}"
            )


def test_starts_a_sentence_rejects_negated_lead_in() -> None:
    """Falsifiability: prepending a negating lead-in breaks the sentence pin."""
    sample = "Unless The owner-approval checks of those infra2 paths are part of the reservation itself."
    assert not starts_a_sentence(
        sample,
        "The owner-approval checks of those infra2 paths are part of the reservation itself.",
    )
