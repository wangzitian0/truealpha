from typing import Literal

from infra2_sdk.runtime.environment import RuntimeEnvKey
from pydantic import AliasChoices, Field
from truealpha_contracts.common import CaptureEnvironment
from truealpha_runtime import RuntimeSettings, database_url_field

LLM_API_KEY_ENV = "LLM_API_KEY"


class Settings(RuntimeSettings):
    """Data-source settings layered on the shared runtime contract."""

    @property
    def llm_api_key_env(self) -> str:
        return LLM_API_KEY_ENV

    @property
    def capture_environment(self) -> CaptureEnvironment:
        return CaptureEnvironment(self.environment_tier.value)

    release_manifest_id: str = Field(
        default="",
        validation_alias=AliasChoices(RuntimeEnvKey.RELEASE_ID.value, "RELEASE_MANIFEST_ID"),
        json_schema_extra={"source": "release", "group": "release"},
    )
    data_engine_image_digest: str = Field(
        default="",
        validation_alias=RuntimeEnvKey.IMAGE_DIGEST.value,
        json_schema_extra={"source": "release", "group": "release"},
    )
    configuration_sha256: str = Field(
        default="",
        validation_alias=RuntimeEnvKey.CONFIGURATION_SHA256.value,
        json_schema_extra={"source": "release", "group": "release"},
    )
    capture_approved_by: str = Field(
        default="",
        validation_alias=AliasChoices("TRUEALPHA_CAPTURE_APPROVED_BY", "CAPTURE_APPROVED_BY"),
        json_schema_extra={"source": "decision", "group": "release"},
    )
    database_url: str = database_url_field(
        composed_from="postgresql://postgres:{POSTGRES_PASSWORD}@127.0.0.1:{env:TA_POSTGRES_PORT}/truealpha",
    )
    sec_user_agent: str = Field(
        default="", json_schema_extra={"source": "human", "injected": True, "scope": "project", "group": "sec"}
    )
    moomoo_opend_host: str = Field(
        default="", json_schema_extra={"source": "code", "empty_ok": True, "group": "moomoo"}
    )
    moomoo_opend_port: int = Field(default=0, json_schema_extra={"source": "code", "group": "moomoo"})
    moomoo_kline_origin_enabled: bool = Field(default=False, json_schema_extra={"source": "code", "group": "moomoo"})
    moomoo_financials_origin_enabled: bool = Field(
        default=False, json_schema_extra={"source": "code", "group": "moomoo"}
    )
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
    external_call_ledger: Literal["postgres", "off"] = Field(
        default="postgres", json_schema_extra={"source": "code", "group": "runtime"}
    )
    llm_api_key: str = Field(
        default="",
        validation_alias=LLM_API_KEY_ENV,
        json_schema_extra={"source": "human", "scope": "project", "empty_ok": True, "sensitive": True, "group": "llm"},
    )
    llm_base_url: str = Field(
        default="https://open.bigmodel.cn/api/coding/paas/v4", json_schema_extra={"source": "code", "group": "llm"}
    )
    llm_model: str = Field(default="glm-5.3-flash", json_schema_extra={"source": "code", "group": "llm"})
    llm_provider: str = Field(default="zhipu-glm-coding-plan", json_schema_extra={"source": "code", "group": "llm"})


settings = Settings()

__all__ = ["RuntimeEnvKey", "Settings", "settings"]
