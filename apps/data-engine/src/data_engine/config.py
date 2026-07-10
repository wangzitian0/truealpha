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
    # defensive throttle/audit trail (init.md Section 1 rule 6).
    moomoo_monthly_call_budget: int = 2000


settings = Settings()
