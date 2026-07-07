from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Read from environment / repo-root .env (see .env.example)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "dev"  # dev | staging | prod
    database_url: str = "postgresql://postgres:postgres@localhost:5432/truealpha"
    # SEC requires a descriptive User-Agent including a contact email.
    sec_user_agent: str = ""


settings = Settings()
