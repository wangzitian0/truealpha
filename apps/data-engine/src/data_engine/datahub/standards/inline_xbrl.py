"""What a filing tags for machines: numeric facts and their contexts, read from inline XBRL.

An annual filing is read twice over: once as prose (`filing_plain_text`) and once as tagged
facts, because the two carry different strength. A sentence can mention "one operating segment"
about anything (#822); a tag asserts a value, for a period, about the whole filer or a named part
of it. This module reads the tags and nothing else — which concepts matter, and what to conclude
from them, is the adapter's.

A regex reader rather than an XML parser, on purpose: an SEC primary document is HTML that embeds
XBRL elements and is frequently not well-formed XML, while the few elements read here
(`xbrli:context`, `ix:nonFraction`, `ix:nonNumeric` with its `ix:continuation` chain, `ix:header`)
have a fixed, machine-generated shape. Anything it cannot read comes back as unreadable rather
than guessed, so a reader failure costs a refusal.
"""

from __future__ import annotations

import html
import re
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import cached_property

from data_engine.datahub.standards.filing_extraction import filing_plain_text

_CONTEXT = re.compile(
    r"<xbrli:context\b[^>]*\bid\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</xbrli:context\s*>",
    re.IGNORECASE | re.DOTALL,
)
_START = re.compile(r"<xbrli:startDate>\s*(\d{4}-\d{2}-\d{2})\s*<", re.IGNORECASE)
_END = re.compile(r"<xbrli:(?:endDate|instant)>\s*(\d{4}-\d{2}-\d{2})\s*<", re.IGNORECASE)
_EXPLICIT_MEMBER = re.compile(
    r"<xbrldi:explicitMember\b[^>]*\bdimension\s*=\s*[\"']([^\"']+)[\"'][^>]*>\s*([^<\s]+)\s*<",
    re.IGNORECASE,
)
_TYPED_MEMBER = re.compile(r"<xbrldi:typedMember\b", re.IGNORECASE)
_NON_FRACTION = re.compile(r"<ix:nonFraction\b[^>]*>", re.IGNORECASE)
_NON_FRACTION_END = re.compile(r"</ix:nonFraction\s*>", re.IGNORECASE)
_NON_NUMERIC = re.compile(r"<ix:nonNumeric\b[^>]*>", re.IGNORECASE)
#: A text block's body is not where its tag is. The `ix:nonNumeric` holds the heading (AVGO's
#: segment note: "Segment Information", 20 bytes) and names, in `continuedAt`, the
#: `ix:continuation` carrying the next part, which names the next. Both elements nest — a policy
#: block inside a note, a fact inside either — so an element's close is found by depth over both.
_TEXT_PART = re.compile(r"<(/?)ix:(?:nonNumeric|continuation)\b[^>]*>", re.IGNORECASE)
_ATTRIBUTE = re.compile(r"([\w:.-]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')")
#: The hidden header: where the contexts live, and where a filer may tag a value it never prints.
_HIDDEN_HEADER = re.compile(r"<ix:header\b.*?</ix:header\s*>", re.IGNORECASE | re.DOTALL)
_MARKUP = re.compile(r"<[^>]+>")
#: `ixt-sec:numwordsen` is how a filer tags a word ("one reportable segment").
_NUMBER_WORDS = {
    "no": 0,
    "none": 0,
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}
#: Marks the tag's position before markup is stripped: the printed value ("one", "36,858")
#: occurs many times in a filing, and only the mark says which occurrence was tagged.
_MARK = "⦃tagged-fact⦄"
#: A typed dimension is not resolved here. It is recorded so the context can never be mistaken
#: for one about the whole filer.
TYPED_DIMENSION = ("typed", "")


@dataclass(frozen=True)
class Context:
    start: date | None
    end: date
    #: (axis, member) pairs, sorted; empty for a fact about the whole filer.
    dimensions: tuple[tuple[str, str], ...]

    @property
    def days(self) -> int | None:
        """The duration, or None for an instant."""
        return None if self.start is None else (self.end - self.start).days


@dataclass(frozen=True)
class TaggedFact:
    concept: str
    context: Context
    #: As tagged: the `scale` and `sign` attributes applied. None when the shown text cannot be
    #: read as a number, so the caller refuses instead of reading a wrong one.
    value: Decimal | None
    scale: int
    offset: int
    hidden: bool


class InlineXbrl:
    """One filing's tagged numeric facts, parsed once and shared by every reader of it."""

    def __init__(self, body: bytes) -> None:
        self._raw = body.decode("utf-8", "ignore")

    @cached_property
    def _hidden(self) -> tuple[tuple[int, int], ...]:
        return tuple(match.span() for match in _HIDDEN_HEADER.finditer(self._raw))

    @cached_property
    def contexts(self) -> dict[str, Context]:
        parsed: dict[str, Context] = {}
        for context_id, inner in _CONTEXT.findall(self._raw):
            ends = _END.findall(inner)
            if not ends:
                continue
            starts = _START.findall(inner)
            dimensions = sorted(_EXPLICIT_MEMBER.findall(inner))
            if _TYPED_MEMBER.search(inner):
                dimensions.append(TYPED_DIMENSION)
            parsed[context_id] = Context(
                start=date.fromisoformat(starts[-1]) if starts else None,
                end=date.fromisoformat(ends[-1]),
                dimensions=tuple(dimensions),
            )
        return parsed

    def facts(self, concepts: Collection[str]) -> list[TaggedFact]:
        """Every `ix:nonFraction` tagged with one of `concepts`, in document order."""
        found = []
        for tag in _NON_FRACTION.finditer(self._raw):
            attributes = _attributes(tag.group(0))
            concept = attributes.get("name", "")
            context = self.contexts.get(attributes.get("contextRef", ""))
            if concept not in concepts or context is None:
                continue
            # To the NEXT close, with markup stripped: a filer tagging one printed value with two
            # concepts nests one tag inside the other (DUOL, SHOP), and both read the same value.
            end = _NON_FRACTION_END.search(self._raw, tag.end())
            shown = html.unescape(_MARKUP.sub(" ", self._raw[tag.end() : end.start() if end else tag.end()]))
            scale = _integer(attributes.get("scale", "0"))
            found.append(
                TaggedFact(
                    concept=concept,
                    context=context,
                    value=_number(shown, attributes.get("format", ""), scale, attributes.get("sign") == "-"),
                    scale=scale or 0,
                    offset=tag.start(),
                    hidden=any(start <= tag.start() < stop for start, stop in self._hidden),
                )
            )
        return found

    def sentence_at(self, offset: int, *, lead: int, span: int) -> str | None:
        """The printed text around the tag at `offset`: `lead` characters before it, `span` after.

        The fragment starts and ends on a tag boundary so no half-tag reads as text, and never
        reaches back into a hidden header.
        """
        start = max([offset - 6000] + [stop for _, stop in self._hidden if stop <= offset])
        start = self._raw.find("<", max(0, start))
        stop = self._raw.rfind(">", offset, offset + 3000) + 1
        fragment = f"{self._raw[start:offset]} {_MARK} {self._raw[offset:stop]}"
        text = filing_plain_text(fragment.encode("utf-8"))
        at = text.find(_MARK)
        if at < 0:
            return None
        before = text[max(0, at - lead) : at]
        return " ".join((before + text[at + len(_MARK) : at + len(_MARK) + span]).split())

    def text_block(self, concept: str, *, limit: int) -> str | None:
        """The printed text of the first block tagged `concept` about the whole filer — markup
        stripped, whitespace collapsed, at most `limit` characters — or None when it tags none.

        A block about a part of the filer (a subsidiary registrant's note in a combined filing)
        is skipped for the same reason a dimensional count is. The continuation chain is
        followed only as far as `limit` needs, and a malformed chain stops where it goes wrong:
        a part that never closes is unreadable and ends the block with what was read before it,
        and a chain that names a part already read ends there rather than being read again.
        """
        for tag in _NON_NUMERIC.finditer(self._raw):
            attributes = _attributes(tag.group(0))
            context = self.contexts.get(attributes.get("contextRef", ""))
            if attributes.get("name") != concept or context is None or context.dimensions:
                continue
            parts: list[str] = []
            length = 0
            read: set[str] = set()
            part: re.Match[str] | None = tag
            while part is not None and length < limit:
                body = self._element_body(part)
                if body is None:
                    break
                text = " ".join(html.unescape(_MARKUP.sub(" ", body)).split())
                if text:  # a part holding only markup (a page break) is neither a word gap nor length
                    length += len(text) + (1 if parts else 0)  # exactly len(" ".join(parts))
                    parts.append(text)
                following = _attributes(part.group(0)).get("continuedAt")
                if not following or following in read:
                    break
                read.add(following)
                part = self._continuation(following)
            return " ".join(parts)[:limit] or None
        return None

    def _element_body(self, opening: re.Match[str]) -> str | None:
        """The markup between an opening tag and its own close, or None when it never closes —
        an unclosed element would otherwise read as the rest of the document."""
        depth = 1
        for tag in _TEXT_PART.finditer(self._raw, opening.end()):
            depth += -1 if tag.group(1) else 1
            if depth == 0:
                return self._raw[opening.end() : tag.start()]
        return None

    def _continuation(self, part_id: str) -> re.Match[str] | None:
        return re.search(
            rf"<ix:continuation\b[^>]*\bid\s*=\s*[\"']{re.escape(part_id)}[\"'][^>]*>", self._raw, re.IGNORECASE
        )


def _attributes(tag: str) -> dict[str, str]:
    return {name: double or single for name, double, single in _ATTRIBUTE.findall(tag)}


def _integer(text: str) -> int | None:
    try:
        return int(text)
    except ValueError:
        return None


def _number(shown: str, format_name: str, scale: int | None, negative: bool) -> Decimal | None:
    if scale is None:
        return None
    text = " ".join(shown.split())
    if format_name.endswith("fixed-zero") or text in {"-", "—", "–"}:
        value = Decimal(0)
    elif text.lower() in _NUMBER_WORDS:
        value = Decimal(_NUMBER_WORDS[text.lower()])
    else:
        # `ixt:num-comma-decimal` writes "1.234,5"; every other numeric format uses a comma, if
        # anything, as the thousands separator.
        digits = text.replace(".", "").replace(",", ".") if "comma-decimal" in format_name else text.replace(",", "")
        try:
            value = Decimal(digits.replace(" ", ""))
        except InvalidOperation:
            return None
        if not value.is_finite():
            return None
    value = value * (Decimal(10) ** scale)
    return -value if negative else value
