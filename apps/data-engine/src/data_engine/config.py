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

    # SEC requires a descriptive User-Agent including a contact email.
    sec_user_agent: str = ""
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
    moomoo_opend_host: str = ""
    moomoo_opend_port: int = 0
    # Self-imposed precautionary cap, NOT a real moomoo-side monthly quota —
    # moomoo's own docs only rate-limit fundamental/quote endpoints (bursts
    # per 30s); see init.md Section 5's 2026-07-10 correction. Kept as a
    # defensive runaway backstop / audit trail (init.md Section 1 rule 6),
    # sized so one full-universe fundamental sweep (~1,200 listing lines x
    # 9 core endpoints ≈ 11k calls) fits with headroom.
    moomoo_monthly_call_budget: int = 20000
    # Which ledger the gate reads/writes: 'json' (local file, Phase -1 probe
    # scripts) or 'postgres' (staging.api_call_ledger — required for sweeps).
    moomoo_ledger_backend: str = "json"
    # Process-local burst throttle matching moomoo's real limit shape
    # (bursts per 30s). Global across endpoints, deliberately conservative.
    # ge=1: 0 would make throttle() index an empty deque instead of meaning
    # "no throttle" — misconfiguration must fail at startup, not mid-sweep.
    moomoo_calls_per_30s: int = Field(default=8, ge=1)
    # Optional; raises OpenFIGI mapping limits from 25 req/min x 10 jobs to
    # 25 req/6s x 100 jobs. Free key: https://www.openfigi.com/api
    openfigi_api_key: str = ""
    # Optional independent second price origin (#344, init.md rule 15). Rendered
    # from Vault by infra2's truealpha/20.data_engine/secrets.ctmpl. Without it
    # every market-price cell is honestly single-origin and reconciliation
    # reports insufficient_independent_origins rather than silently agreeing.
    twelve_data_api_key: str = ""


settings = Settings()
