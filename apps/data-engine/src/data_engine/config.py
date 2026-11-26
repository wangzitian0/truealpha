from typing import Literal

from infra2_sdk.config import RuntimeEnvKey
from infra2_sdk.llm import LlmSettings
from truealpha_contracts.common import CaptureEnvironment
from truealpha_runtime import RuntimeSettings, database_url_field
from truealpha_runtime.config import _schema_field as _f

LLM_API_KEY_ENV = "LLM_API_KEY"
_LLM_DEFAULTS = LlmSettings()


class Settings(RuntimeSettings):
    """Data-source settings layered on the runtime contract."""

    @property
    def llm_api_key_env(self) -> str:
        return LLM_API_KEY_ENV

    @property
    def capture_environment(self) -> CaptureEnvironment:
        return CaptureEnvironment(self.environment_tier.value)

    # Retained for expand-contract parity with infra2 20.data_engine and prod v0.3.6.
    release_id: str = _f("", "release", "release")
    image_digest: str = _f("", "release", "release")
    configuration_sha256: str = _f("", "release", "release", validation_alias=RuntimeEnvKey.CONFIGURATION_SHA256.value)
    capture_approved_by: str = _f(
        "",
        "decision",
        "release",
        validation_alias="CAPTURE_APPROVED_BY",
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
    llm_base_url: str = _f(_LLM_DEFAULTS.base_url, "code", "llm")
    llm_small_model: str = _f(_LLM_DEFAULTS.small_model, "code", "llm")
    llm_provider: str = _f(_LLM_DEFAULTS.provider, "code", "llm")
    # Dagster reads these two names itself; the Settings model only declares them in the manifest.
    # The image sets DAGSTER_HOME (Dockerfile). The infra2 entrypoint derives DAGSTER_POSTGRES_URL
    # from DATABASE_URL with the search path `dagster` (dagster.yaml).
    dagster_home: str = _f("/opt/dagster", "code", "dagster")
    dagster_postgres_url: str = _f("", "runtime", "dagster", empty_ok=True, sensitive=True)
    # The compose gives the webserver role this port; the image CMD uses 3001 (Dockerfile).
    dagster_webserver_port: int = _f(3001, "code", "dagster")
    # infra2_sdk.telemetry.init_worker reads these three names itself (dagster_defs.py).
    # An empty endpoint keeps telemetry off. The service name defaults to "truealpha".
    otel_exporter_otlp_endpoint: str = _f("", "code", "otel", empty_ok=True)
    otel_service_name: str = _f("truealpha", "code", "otel")
    otel_resource_attributes: str = _f("", "code", "otel", empty_ok=True)


settings = Settings()

__all__ = ["RuntimeEnvKey", "Settings", "settings"]
