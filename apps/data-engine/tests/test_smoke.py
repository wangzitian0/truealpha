from data_engine.config import Settings
from data_engine.sources.sec import COMPANY_FACTS_URL


def test_settings_defaults(monkeypatch):
    # _env_file=None only isolates the .env FILE; ambient variables (CI sets
    # APP_ENV=ci) still win — strip them so this genuinely tests the defaults.
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    s = Settings(_env_file=None)
    assert s.app_env == "dev"
    assert s.sec_user_agent == ""
    assert s.llm_model == "glm-5.3-flash"


def test_cik_url_is_zero_padded():
    assert COMPANY_FACTS_URL.format(cik=1561550).endswith("CIK0001561550.json")
