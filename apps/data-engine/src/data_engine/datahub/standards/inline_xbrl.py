"""Inline XBRL context, numeric-fact, and text-block parser for SEC primary HTML filings."""

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
_TEXT_PART = re.compile(r"<(/?)ix:(?:nonNumeric|continuation)\b[^>]*>", re.IGNORECASE)
_ATTRIBUTE = re.compile(r"([\w:.-]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')")
_HIDDEN_HEADER = re.compile(r"<ix:header\b.*?</ix:header\s*>", re.IGNORECASE | re.DOTALL)
_MARKUP = re.compile(r"<[^>]+>")
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
_MARK = "⦃tagged-fact⦄"
TYPED_DIMENSION = ("typed", "")


@dataclass(frozen=True)
class Context:
    start: date | None
    end: date
    dimensions: tuple[tuple[str, str], ...]

    @property
    def days(self) -> int | None:
        return None if self.start is None else (self.end - self.start).days


@dataclass(frozen=True)
class TaggedFact:
    concept: str
    context: Context
    value: Decimal | None
    scale: int
    offset: int
    hidden: bool


@dataclass(frozen=True)
class TextFact:
    concept: str
    context_id: str
    text: str


class InlineXbrl:
    """Parse contexts, `ix:nonFraction` numeric facts, and `ix:nonNumeric` text blocks from an iXBRL document."""

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
        found = []
        for tag in _NON_FRACTION.finditer(self._raw):
            attributes = _attributes(tag.group(0))
            concept = attributes.get("name", "")
            context = self.contexts.get(attributes.get("contextRef", ""))
            if concept not in concepts or context is None:
                continue
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

    def text_facts(self, concepts: Collection[str], *, limit: int) -> list[TextFact]:
        found = []
        for tag in _NON_NUMERIC.finditer(self._raw):
            attributes = _attributes(tag.group(0))
            concept = attributes.get("name", "")
            context_id = attributes.get("contextRef", "")
            if concept not in concepts or not context_id:
                continue
            body = self._element_body(tag)
            if body is None:
                continue
            text = " ".join(html.unescape(_MARKUP.sub(" ", body)).split())[:limit]
            if text:
                found.append(TextFact(concept=concept, context_id=context_id, text=text))
        return found

    def sentence_at(self, offset: int, *, lead: int, span: int) -> str | None:
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
                if text:
                    length += len(text) + (1 if parts else 0)
                    parts.append(text)
                following = _attributes(part.group(0)).get("continuedAt")
                if not following or following in read:
                    break
                read.add(following)
                part = self._continuation(following)
            return " ".join(parts)[:limit] or None
        return None

    def _element_body(self, opening: re.Match[str]) -> str | None:
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
        digits = text.replace(".", "").replace(",", ".") if "comma-decimal" in format_name else text.replace(",", "")
        try:
            value = Decimal(digits.replace(" ", ""))
        except InvalidOperation:
            return None
        if not value.is_finite():
            return None
    value = value * (Decimal(10) ** scale)
    return -value if negative else value
