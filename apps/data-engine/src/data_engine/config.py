from typing import Literal

from pydantic import Field
from truealpha_contracts.common import CaptureEnvironment
from truealpha_runtime import RuntimeSettings


class Settings(RuntimeSettings):
    """Data-source settings layered on the shared runtime contract."""

    @property
    def capture_environment(self) -> CaptureEnvironment:
        """The environment every capture campaign is stamped with (#72).

        Derived from the resolved runtime tier, never a literal: until #72 the
        composition root wrote `PRODUCTION` on every campaign, so staging ticks recorded
        production lineage. The tier vocabulary and the capture vocabulary are the same
        six names by design (init.md §3.1), so an unknown tier raises here instead of
        being stamped as something it is not.
        """
        return CaptureEnvironment(self.environment_tier.value)

    # The shared runtime contract owns DATABASE_URL; this service composes it for the
    # host-network topology (host loopback port), which is why the override lives here.
    database_url: str = Field(
        default="postgresql://postgres:postgres@localhost:5432/truealpha",
        json_schema_extra={
            "source": "runtime",
            "group": "postgres",
            "provided_by": "truealpha/postgres:POSTGRES_PASSWORD",
            # host-network service: dials the host loopback port infra2 publishes
            "composed_from": "postgresql://postgres:{POSTGRES_PASSWORD}@127.0.0.1:{env:TA_POSTGRES_PORT}/truealpha",
        },
    )
    # SEC requires a descriptive User-Agent including a contact email.
    sec_user_agent: str = Field(
        default="", json_schema_extra={"source": "human", "injected": True, "scope": "project", "group": "sec"}
    )
    # moomoo OpenD gateway coordinates. Supplied by the environment ONLY — never a
    # default here, for two reasons that happen to agree:
    #
    # 1. Owner directive: OpenD's location is managed as a secret. Its host, its port and
    #    the fact of where it runs are deployment facts, not source. A tracked default is
    #    a tracked disclosure however innocuous the value looks.
    # 2. The old default was `127.0.0.1`, which is WRONG in every deployed context: inside
    #    a container that is the container's own loopback, not the host's. A default that
    #    cannot be correct anywhere it actually runs is worse than no default -- it turns
    #    "nobody configured this" into a connection attempt against the wrong machine,
    #    which surfaces as a timeout rather than as a missing configuration.
    #
    # Empty means unconfigured, and callers must refuse rather than dial a guess.
    moomoo_opend_host: str = Field(default="", json_schema_extra={"source": "code", "group": "moomoo"})
    moomoo_opend_port: int = Field(default=0, json_schema_extra={"source": "code", "group": "moomoo"})
    # Self-imposed precautionary cap, NOT a real moomoo-side monthly quota —
    # moomoo's own docs only rate-limit fundamental/quote endpoints (bursts
    # per 30s); see init.md Section 5's 2026-07-10 correction. Kept as a
    # defensive runaway backstop / audit trail (init.md Section 1 rule 6),
    # sized so one full-universe fundamental sweep (~1,200 listing lines x
    # 9 core endpoints ≈ 11k calls) fits with headroom.
    moomoo_monthly_call_budget: int = Field(default=20000, json_schema_extra={"source": "code", "group": "moomoo"})
    # Which ledger the gate reads/writes: 'json' (local file, Phase -1 probe
    # scripts) or 'postgres' (staging.api_call_ledger — required for sweeps).
    moomoo_ledger_backend: str = Field(default="json", json_schema_extra={"source": "code", "group": "moomoo"})
    # Process-local burst throttle matching moomoo's real limit shape
    # (bursts per 30s). Global across endpoints, deliberately conservative.
    # ge=1: 0 would make throttle() index an empty deque instead of meaning
    # "no throttle" — misconfiguration must fail at startup, not mid-sweep.
    moomoo_calls_per_30s: int = Field(default=8, ge=1, json_schema_extra={"source": "code", "group": "moomoo"})
    # Optional; raises OpenFIGI mapping limits from 25 req/min x 10 jobs to
    # 25 req/6s x 100 jobs. Free key: https://www.openfigi.com/api
    openfigi_api_key: str = Field(
        default="",
        json_schema_extra={
            "source": "human",
            "scope": "project",
            "empty_ok": True,
            "sensitive": True,
            "group": "vendors",
        },
    )
    # Optional independent second price origin (#344, init.md rule 15). Rendered
    # from Vault by infra2's truealpha/20.data_engine/secrets.ctmpl. Without it
    # every market-price cell is honestly single-origin and reconciliation
    # reports insufficient_independent_origins rather than silently agreeing.
    twelve_data_api_key: str = Field(
        default="",
        json_schema_extra={
            "source": "human",
            "scope": "project",
            "empty_ok": True,
            "sensitive": True,
            "group": "vendors",
        },
    )
    # Where `sources.gateway` writes the external call ledger (#729): 'postgres'
    # (staging.api_call_ledger, autocommit, the only value a deployed process may
    # run with) or 'off' for a local probe with no warehouse at all. There is no
    # silent fallback between the two: a deployed tick that cannot record its
    # vendor calls logs every unrecorded row at ERROR rather than pretending.
    # `Literal` so a misspelt value fails at startup instead of behaving like 'postgres'.
    external_call_ledger: Literal["postgres", "off"] = Field(default="postgres", json_schema_extra={"source": "code"})

    # The filing-extraction model provider (#70 scope 1, owner decision 2026-09-07: the
    # Zhipu GLM coding plan). Empty key = no provider seated: the loop keeps every
    # multi-candidate filing as `needs_model_selection`, never guesses. The base URL is an
    # OpenAI-compatible chat-completions root; the model is a versioned parameter that
    # travels into every invocation record.
    llm_api_key: str = Field(
        default="",
        json_schema_extra={"source": "human", "scope": "project", "empty_ok": True, "sensitive": True, "group": "llm"},
    )
    llm_base_url: str = Field(
        default="https://open.bigmodel.cn/api/coding/paas/v4", json_schema_extra={"source": "code", "group": "llm"}
    )
    llm_model: str = Field(default="glm-4.7", json_schema_extra={"source": "code", "group": "llm"})
    llm_provider: str = Field(default="zhipu-glm-coding-plan", json_schema_extra={"source": "code", "group": "llm"})


settings = Settings()
