from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Read from environment / repo-root .env (see .env.example)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "dev"  # dev | staging | prod
    database_url: str = "postgresql://postgres:postgres@localhost:5432/truealpha"
    # SEC requires a descriptive User-Agent including a contact email.
    sec_user_agent: str = ""
    # moomoo OpenD gateway (must already be running and logged in — see
    # data_engine/sources/moomoo.py). Not the moomoo account itself.
    moomoo_opend_host: str = "127.0.0.1"
    moomoo_opend_port: int = 11111
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
    moomoo_calls_per_30s: int = 8
    # Optional; raises OpenFIGI mapping limits from 25 req/min x 10 jobs to
    # 25 req/6s x 100 jobs. Free key: https://www.openfigi.com/api
    openfigi_api_key: str = ""


settings = Settings()
