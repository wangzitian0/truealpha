"""Publish a concept-mapping ruleset and advance the governed pointer (#496).

This is the operation that replaces "edit a tuple, open a PR, wait for a release": a
corrected mapping becomes an insert into `staging.contract_objects` plus a pointer row.
The running image is untouched, and the next tick resolves the new rules and stamps their
hash into every observation it writes.

Reverting is another advance, not a deletion — so a mapping that turned out wrong is
backed out without losing the record that it was once in force.

Usage:
    # what the running image would resolve today
    uv run --package truealpha-data-engine python apps/data-engine/scripts/publish_concept_mapping.py --show

    # publish the image's default (seeds a fresh environment)
    ... publish_concept_mapping.py --publish-default --note "seed from image v0.0.x"

    # publish an edited ruleset
    ... publish_concept_mapping.py --from-file ruleset.json --note "AVGO tags SalesRevenueNet from FY2026"
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import psycopg
from data_engine.config import settings
from data_engine.datahub.production_topt.concept_mapping import (
    DEFAULT_RULESET,
    publish_ruleset,
    resolve_ruleset,
)
from truealpha_contracts.concept_mapping import ConceptMappingRuleset


def _describe(ruleset: ConceptMappingRuleset) -> None:
    print(f"{ruleset.version}  {ruleset.ruleset_id}")
    for mapping in ruleset.mappings:
        concepts = ", ".join(f"{item.taxonomy}:{item.concept}" for item in mapping.concepts)
        print(f"  {mapping.field:<32} {mapping.kind.value:<9} {concepts}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--show", action="store_true", help="print what a run would resolve right now")
    parser.add_argument("--publish-default", action="store_true", help="publish the ruleset shipped in this image")
    parser.add_argument("--from-file", type=Path, default=None, help="publish an edited ruleset from JSON")
    parser.add_argument("--note", default=None, help="why this ruleset is being accepted (required to publish)")
    args = parser.parse_args()

    if args.show:
        with psycopg.connect(settings.database_url) as connection:
            _describe(resolve_ruleset(connection))
        return 0

    if not (args.publish_default or args.from_file):
        parser.error("nothing to do: pass --show, --publish-default or --from-file")
    if not args.note:
        # The pointer table requires it, and a mapping change with no stated reason is
        # indistinguishable from an accident on a plane that skips code review.
        parser.error("--note is required: say why this ruleset is being accepted")

    ruleset = (
        DEFAULT_RULESET
        if args.publish_default
        else ConceptMappingRuleset.model_validate(json.loads(args.from_file.read_text()))
    )
    _describe(ruleset)
    with psycopg.connect(settings.database_url) as connection:
        contract_id, sequence = publish_ruleset(connection, ruleset, note=args.note)
        connection.commit()
    print(f"accepted as sequence {sequence}: {contract_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
