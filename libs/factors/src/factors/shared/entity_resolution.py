"""Entity resolution over the knowledge graph (staging.kg_entities / staging.kg_edges).

All cross-source ID crosswalk (CIK <-> ticker <-> moomoo_code <-> CUSIP/ISIN) goes
through `same_as` edges here — no module keeps its own mapping table.
Wired to Postgres in Phase 0; the Phase -1 smoke test exercises one sample per
entity type (company, ETF, analyst, supply-chain relationship).
"""

from datetime import datetime


def resolve(source: str, source_id: str, *, as_of: datetime) -> str:
    """Return the unified entity id for a source-local identifier, point-in-time."""
    raise NotImplementedError("Phase 0: read same_as edges from staging.kg_edges")
