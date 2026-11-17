from typing import Literal

from infra2_sdk.runtime.environment import RuntimeEnvKey
from pydantic import AliasChoices
from truealpha_contracts.common import CaptureEnvironment
from truealpha_runtime import RuntimeSettings, database_url_field
from truealpha_runtime.config import _schema_field as _f

LLM_API_KEY_ENV = "LLM_API_KEY"


class Settings(RuntimeSettings):
    """Data-source settings layered on the runtime contract."""

    @property
    def llm_api_key_env(self) -> str:
        return LLM_API_KEY_ENV

    @property
    def capture_environment(self) -> CaptureEnvironment:
        return CaptureEnvironment(self.environment_tier.value)

    release_manifest_id: str = _f(
        "", "release", "release", validation_alias=AliasChoices(RuntimeEnvKey.RELEASE_ID.value, "RELEASE_MANIFEST_ID")
    )
    data_engine_image_digest: str = _f("", "release", "release", validation_alias=RuntimeEnvKey.IMAGE_DIGEST.value)
    configuration_sha256: str = _f("", "release", "release", validation_alias=RuntimeEnvKey.CONFIGURATION_SHA256.value)
    capture_approved_by: str = _f(
        "",
        "decision",
        "release",
        validation_alias=AliasChoices("CAPTURE_APPROVED_BY", "TRUEALPHA_CAPTURE_APPROVED_BY"),
    )
    database_url: str = database_url_field(
        composed_from="postgresql://postgres:{POSTGRES_PASSWORD}@127.0.0.1:{env:TA_POSTGRES_PORT}/truealpha",
    )
    sec_user_agent: str = _f("", "human", "sec", injected=True, scope="project")
    moomoo_opend_host: str = _f("", "code", "moomoo", empty_ok=True)
    moomoo_opend_port: int = _f(0, "code", "moomoo")
    moomoo_kline_origin_enabled: bool = _f(False, "code", "moomoo")
    moomoo_financials_origin_enabled: bool = _f(False, "code", "moomoo")
    openfigi_api_key: str = _f("", "human", "vendors", scope="project", empty_ok=True, sensitive=True)
    twelve_data_api_key: str = _f("", "human", "vendors", scope="project", empty_ok=True, sensitive=True)
    external_call_ledger: Literal["postgres", "off"] = _f("postgres", "code", "runtime")
    llm_api_key: str = _f(
        "", "human", "llm", validation_alias=LLM_API_KEY_ENV, scope="project", empty_ok=True, sensitive=True
    )
    llm_base_url: str = _f("https://open.bigmodel.cn/api/coding/paas/v4", "code", "llm")
    llm_model: str = _f("glm-5.3-flash", "code", "llm")
    llm_provider: str = _f("zhipu-glm-coding-plan", "code", "llm")


settings = Settings()

__all__ = ["RuntimeEnvKey", "Settings", "settings"]
