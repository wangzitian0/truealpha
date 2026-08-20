"""SEC N-PORT-P holdings parser (init.md module 5's confirmed source; #641 audit).

N-PORT is the design's ETF holdings-weight source — the samples were captured
2026-07 and the parser never existed. Holdings carry NO ticker; identity resolves
downstream via ISIN → OpenFIGI → shareClassFIGI, the same `security:figi:*` space
the universe plane already keys on. CUSIP rides along as parsed fact (it is in the
public filing's bytes) but is never an identity here.

Decimal-safe: weights and values never pass through float.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class NportHolding:
    name: str
    title: str
    isin: str | None
    cusip: str | None
    balance: Decimal
    value_usd: Decimal
    weight_pct: Decimal  # pctVal: percentage of fund net assets, as filed


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_nport_holdings(body: bytes) -> list[NportHolding]:
    """Every <invstOrSec> in the filing, in document order. Fails loud on a holding
    missing its weight or value — a partial parse silently understating a fund is
    the #527 green-while-empty shape."""
    root = ET.fromstring(body)
    holdings: list[NportHolding] = []
    for element in root.iter():
        if _local(element.tag) != "invstOrSec":
            continue
        fields: dict[str, str] = {}
        isin: str | None = None
        for child in element.iter():
            name = _local(child.tag)
            if name == "isin":
                isin = (child.get("value") or "").strip() or None
            elif child.text and child.text.strip():
                fields.setdefault(name, child.text.strip())
        try:
            holdings.append(
                NportHolding(
                    name=fields["name"],
                    title=fields.get("title", fields["name"]),
                    isin=isin,
                    cusip=fields.get("cusip"),
                    balance=Decimal(fields["balance"]),
                    value_usd=Decimal(fields["valUSD"]),
                    weight_pct=Decimal(fields["pctVal"]),
                )
            )
        except KeyError as missing:
            raise ValueError(f"N-PORT holding missing required field {missing}: {fields.get('name', '?')}") from None
    if not holdings:
        raise ValueError("N-PORT filing contains no holdings — wrong document or format drift")
    return holdings
