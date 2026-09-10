"""Isolated DataHub control-plane implementation.

Deliberately does NOT re-export the replay harnesses (`tiny_replay`,
`medium_replay`, `hardening_replay`). It did until #795, which meant every
importer of this package — the deployed tick included — executed 1,615 lines of
replay machinery it never calls, purely to reach one corpus function that has
since moved to `production_topt.universe_corpus`. A caller that genuinely wants a
replay imports its module by name; `test_deployed_closure_excludes_replay` is the
standing check that the composition root does not."""

from data_engine.datahub.control_plane import AttemptLedger, expand_obligations
from data_engine.datahub.evidence_graph_repository import PostgresEvidenceGraphRepository
from data_engine.datahub.production_topt.materialization import (
    PostgresToptCoreRepository,
    ToptCoreIdentity,
    ToptCoreMetaInfo,
    ToptCoreReadResult,
    ToptCoreSnapshot,
)
from data_engine.datahub.repository import (
    CaptureRepositoryConflictError,
    PostgresCaptureControlRepository,
    ToptCaptureMetaInfo,
    ToptCaptureStatus,
)
from data_engine.datahub.topt_read import PostgresToptReadRepository

__all__ = [
    "AttemptLedger",
    "CaptureRepositoryConflictError",
    "PostgresEvidenceGraphRepository",
    "PostgresToptReadRepository",
    "PostgresCaptureControlRepository",
    "PostgresToptCoreRepository",
    "ToptCaptureMetaInfo",
    "ToptCaptureStatus",
    "ToptCoreIdentity",
    "ToptCoreMetaInfo",
    "ToptCoreReadResult",
    "ToptCoreSnapshot",
    "expand_obligations",
    "replay_resume_scenarios",
    "select_recapture",
]
