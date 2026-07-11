import pytest
from data_engine.config import Settings
from data_engine.sources.sec import COMPANY_FACTS_URL
from pydantic import ValidationError


def test_settings_defaults():
    s = Settings(_env_file=None)
    assert s.app_env == "dev"
    assert s.sec_user_agent == ""


def test_throttle_rate_must_be_positive():
    # 0 would make throttle() index an empty deque instead of meaning
    # "unthrottled" — misconfiguration fails at startup, not mid-sweep.
    with pytest.raises(ValidationError):
        Settings(_env_file=None, moomoo_calls_per_30s=0)


def test_cik_url_is_zero_padded():
    assert COMPANY_FACTS_URL.format(cik=1561550).endswith("CIK0001561550.json")
