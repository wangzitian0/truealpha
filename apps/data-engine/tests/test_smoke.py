from data_engine.config import Settings
from data_engine.sources.sec import COMPANY_FACTS_URL


def test_settings_defaults():
    s = Settings(_env_file=None)
    assert s.app_env == "dev"
    assert s.sec_user_agent == ""


def test_cik_url_is_zero_padded():
    assert COMPANY_FACTS_URL.format(cik=1561550).endswith("CIK0001561550.json")
