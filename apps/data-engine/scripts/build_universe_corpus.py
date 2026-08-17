"""Build a frozen universe corpus from resolved constituents (#539 QQQ expansion).

Admission-checklist tooling (#579): a universe corpus is versioned scope
configuration, and this script is its auditable producer. Identifier scheme for
universes beyond the hand-curated TOPT 20:

- ``issuer:cik:{10d}``   — SEC CIK, the authority the capture pipeline already
  resolves headcounts, predecessors and company-facts through;
- ``security:figi:{id}`` — OpenFIGI share-class FIGI (an open standard; CUSIP is
  licensed data and deliberately absent);
- ``listing:{mic}:{ticker}`` — same shape the TOPT corpus uses.

Input: a JSON list of {ticker, name, cik, figi} (see the QQQ build notes on the
issue). Output: a package-data corpus with the ``topt_denominator`` shape the
composition root loads, self-pinned by its own instrument-mapping sha256.

Usage:
    uv run --package truealpha-data-engine python apps/data-engine/scripts/build_universe_corpus.py \
        --constituents qqq_enriched.json --universe qqq-us --report-date 2026-06-30 \
        --label "Nasdaq-100 (QQQ) constituents" --out corpus.qqq.v1.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from truealpha_contracts import canonical_sha256

_TUPLE_FIELDS = ["issuer_id", "instrument_id", "listing_id", "ticker"]


def build(constituents: list[dict], *, universe: str, report_date: str, label: str) -> dict:
    rows = sorted(constituents, key=lambda c: c["ticker"])
    instruments = [
        [
            f"issuer:cik:{int(c['cik']):010d}",
            f"security:figi:{c['figi'].lower()}",
            f"listing:xnas:{c['ticker'].lower()}",
            c["ticker"],
        ]
        for c in rows
    ]
    listings = [row[2] for row in instruments]
    if len(set(listings)) != len(listings):
        raise ValueError("duplicate listings in constituents")
    figis = [row[1] for row in instruments]
    if len(set(figis)) != len(figis):
        raise ValueError("duplicate FIGIs in constituents")
    issuer_ids = {row[0] for row in instruments}
    mapping_sha256 = canonical_sha256({"fields": _TUPLE_FIELDS, "instruments": instruments})
    denominator = {
        "universe_id": f"universe:{universe}-{report_date}",
        "list_version_id": "",  # computed by the loader from contract identity
        "list_label": label,
        "accession": None,
        "report_date": report_date,
        "issuer_count": len(issuer_ids),
        "instrument_count": len(instruments),
        "obligation_count": len(instruments) * 4,
        "obligation_expansion": "four-semantics:v1",
        "instrument_tuple_fields": _TUPLE_FIELDS,
        "instruments": instruments,
        "instrument_mapping_sha256": mapping_sha256,
        "identity_assertions": [],
    }
    return {
        "schema_version": 1,
        "corpus_id": canonical_sha256(denominator)[:32],
        "fixture_kind": "frozen-universe-scope (not input data; init.md rule 13)",
        "topt_denominator": denominator,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--constituents", required=True)
    parser.add_argument("--universe", required=True)
    parser.add_argument("--report-date", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    constituents = json.loads(Path(args.constituents).read_text())
    corpus = build(constituents, universe=args.universe, report_date=args.report_date, label=args.label)
    out = Path(__file__).resolve().parents[1] / "src" / "data_engine" / "datahub" / "data" / args.out
    out.write_text(json.dumps(corpus, indent=1, sort_keys=True) + "\n")
    denominator = corpus["topt_denominator"]
    print(
        f"{out.name}: {denominator['issuer_count']} issuers, {denominator['instrument_count']} instruments, "
        f"{denominator['obligation_count']} obligations, mapping {denominator['instrument_mapping_sha256'][:12]}…"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
