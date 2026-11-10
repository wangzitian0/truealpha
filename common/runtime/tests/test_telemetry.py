# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import logging; from collections.abc import Iterator; from typing import Any; import pytest; from opentelemetry.exporter.otlp.proto.http import _log_exporter, metric_exporter, trace_exporter; from opentelemetry.instrumentation.logging.handler import LoggingHandler; from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter; from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult; from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter; from truealpha_runtime.telemetry import ( REQUIRED_IDENTITY_ATTRIBUTES, TelemetryConfigError, init_telemetry, load_telemetry_settings, ); ENDPOINT = "http://platform-signoz-otel-collector:4318"; IAC_REF = "0123456789abcdef0123456789abcdef01234567"; IDENTITY = { "deployment.environment.name": "staging", "infra.service.id": "truealpha/app", "service.version": "v1.2.3", "infra.iac.ref": IAC_REF, }
def _environ(**overrides: str | None) -> dict[str, str]:
    environ = { "OTEL_EXPORTER_OTLP_ENDPOINT": ENDPOINT, "OTEL_SERVICE_NAME": "truealpha-app", "APP_ENV": "staging", "OTEL_RESOURCE_ATTRIBUTES": ",".join(f"{key}={value}" for key, value in IDENTITY.items()), }
    for name, value in overrides.items():
        if value is None: environ.pop(name)
        else: environ[name] = value
    return environ
class _SpanSink(InMemorySpanExporter):
    created: list[_SpanSink] = []
    def __init__(self, endpoint: str | None = None, **_: Any) -> None: super().__init__(); self.endpoint = endpoint; _SpanSink.created.append(self)
class _LogSink(InMemoryLogRecordExporter):
    created: list[_LogSink] = []
    def __init__(self, endpoint: str | None = None, **_: Any) -> None: super().__init__(); self.endpoint = endpoint; _LogSink.created.append(self)
class _MetricSink(MetricExporter):
    created: list[_MetricSink] = []
    def __init__(self, endpoint: str | None = None, **_: Any) -> None: super().__init__(); self.endpoint = endpoint; self.batches: list[Any] = []; _MetricSink.created.append(self)
    def export(self, metrics_data: Any, timeout_millis: float = 10_000, **_: Any) -> MetricExportResult: self.batches.append(metrics_data); return MetricExportResult.SUCCESS
    def force_flush(self, timeout_millis: float = 10_000) -> bool: return True
    def shutdown(self, timeout_millis: float = 30_000, **_: Any) -> None: return None
@pytest.fixture(autouse=True)
def sinks(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for sink in (_SpanSink, _LogSink, _MetricSink): sink.created = []
    monkeypatch.setattr(trace_exporter, "OTLPSpanExporter", _SpanSink); monkeypatch.setattr(metric_exporter, "OTLPMetricExporter", _MetricSink); monkeypatch.setattr(_log_exporter, "OTLPLogExporter", _LogSink); yield
def _exporters_created() -> int: return len(_SpanSink.created) + len(_LogSink.created) + len(_MetricSink.created)
def test_no_endpoint_means_no_telemetry_and_no_exporter() -> None: assert load_telemetry_settings({}) is None; assert init_telemetry({}) is None; assert init_telemetry(_environ(OTEL_EXPORTER_OTLP_ENDPOINT=None)) is None; assert _exporters_created() == 0
def test_an_empty_endpoint_is_no_endpoint() -> None: assert init_telemetry(_environ(OTEL_EXPORTER_OTLP_ENDPOINT="  ")) is None; assert _exporters_created() == 0
def test_the_standard_disable_switch_wins_over_an_endpoint() -> None: bare = {"OTEL_EXPORTER_OTLP_ENDPOINT": ENDPOINT, "OTEL_SDK_DISABLED": "true"}; assert init_telemetry(bare) is None; assert init_telemetry(_environ(OTEL_SDK_DISABLED="TRUE")) is None; assert _exporters_created() == 0
def test_the_process_environment_is_the_default_source(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ( "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_SDK_DISABLED", "OTEL_SERVICE_NAME", "OTEL_RESOURCE_ATTRIBUTES", ): monkeypatch.delenv(name, raising=False)
    assert init_telemetry() is None; monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", ENDPOINT); monkeypatch.delenv("APP_ENV", raising=False)
    with pytest.raises(TelemetryConfigError, match="OTEL_SERVICE_NAME"): init_telemetry()
@pytest.mark.parametrize("key", REQUIRED_IDENTITY_ATTRIBUTES)
def test_a_missing_identity_attribute_refuses_telemetry_naming_only_that_key(key: str) -> None:
    attributes = ",".join(f"{name}={value}" for name, value in IDENTITY.items() if name != key)
    with pytest.raises(TelemetryConfigError) as failure: init_telemetry(_environ(OTEL_RESOURCE_ATTRIBUTES=attributes))
    message = str(failure.value); assert key in message
    for other in REQUIRED_IDENTITY_ATTRIBUTES: assert other == key or other not in message, f"{other} is present and must not be reported"
    assert _exporters_created() == 0, "a refused configuration must not leave exporters running"
def test_the_identity_that_is_missing_altogether_names_all_four() -> None:
    with pytest.raises(TelemetryConfigError) as failure: init_telemetry(_environ(OTEL_RESOURCE_ATTRIBUTES=None))
    assert all(key in str(failure.value) for key in REQUIRED_IDENTITY_ATTRIBUTES)
def test_a_blank_identity_value_counts_as_missing() -> None:
    blank = ",".join(f"{name}={'%20' if name == 'infra.iac.ref' else value}" for name, value in IDENTITY.items())
    with pytest.raises(TelemetryConfigError, match=r"infra\.iac\.ref"): init_telemetry(_environ(OTEL_RESOURCE_ATTRIBUTES=blank))
def test_a_missing_service_name_refuses_telemetry() -> None:
    with pytest.raises(TelemetryConfigError, match="OTEL_SERVICE_NAME"): init_telemetry(_environ(OTEL_SERVICE_NAME=None))
    assert _exporters_created() == 0
def test_the_removed_service_name_alias_does_not_stand_in_for_the_standard_one() -> None:
    with pytest.raises(TelemetryConfigError, match="OTEL_SERVICE_NAME"): init_telemetry(_environ(OTEL_SERVICE_NAME=None, SERVICE_NAME="truealpha-dagster"), set_global=False)
def test_a_legacy_deployment_environment_key_does_not_stand_in_for_the_standard_one() -> None:
    legacy = ",".join( f"{'deployment.environment' if name == 'deployment.environment.name' else name}={value}" for name, value in IDENTITY.items() )
    with pytest.raises(TelemetryConfigError, match=r"deployment\.environment\.name"): init_telemetry(_environ(OTEL_RESOURCE_ATTRIBUTES=legacy))
def _issued_attributes(deployment_environment: str) -> str: return ",".join( f"{key}={deployment_environment if key == 'deployment.environment.name' else value}" for key, value in IDENTITY.items() )
def _exported_deployment_environment(environ: dict[str, str]) -> str:
    providers = init_telemetry(environ, set_global=False); assert providers is not None, "telemetry stayed off although an endpoint was configured"
    try: return str(providers.tracer_provider.resource.attributes["deployment.environment.name"])
    finally: providers.shutdown()
@pytest.mark.parametrize("alias", ["pr-12", "branch-main", "commit-abc1234", "tag-v1-2-3", "canary-preview"])
def test_a_preview_stack_starts_with_its_alias_although_app_env_says_staging(alias: str) -> None: environ = _environ(APP_ENV="staging", OTEL_RESOURCE_ATTRIBUTES=_issued_attributes(alias)); assert _exported_deployment_environment(environ) == alias
@pytest.mark.parametrize("tier", ["staging", "production"])
def test_the_protected_environments_start_under_their_own_name(tier: str) -> None: environ = _environ(APP_ENV=tier, OTEL_RESOURCE_ATTRIBUTES=_issued_attributes(tier)); assert _exported_deployment_environment(environ) == tier
def test_app_env_does_not_decide_whether_telemetry_starts_or_what_it_says() -> None: issued = _issued_attributes("production"); assert _exported_deployment_environment(_environ(APP_ENV=None, OTEL_RESOURCE_ATTRIBUTES=issued)) == "production"; assert _exported_deployment_environment(_environ(APP_ENV="dev", OTEL_RESOURCE_ATTRIBUTES=issued)) == "production"
def test_an_issued_name_the_sdk_does_not_know_is_refused() -> None:
    with pytest.raises(TelemetryConfigError, match="unknown environment"): init_telemetry(_environ(OTEL_RESOURCE_ATTRIBUTES=_issued_attributes("banana")))
    assert _exporters_created() == 0
@pytest.mark.parametrize( ("attributes", "reason"), [("deployment.environment.name", "key=value"), ("deployment.environment.name=%zz", "percent escape")], )
def test_a_malformed_attribute_string_is_refused_by_its_own_reason(attributes: str, reason: str) -> None:
    with pytest.raises(TelemetryConfigError, match=reason): init_telemetry(_environ(APP_ENV=None, OTEL_RESOURCE_ATTRIBUTES=attributes))
    assert _exporters_created() == 0
def test_the_refusal_names_variables_and_never_their_values() -> None:
    with pytest.raises(TelemetryConfigError) as failure: init_telemetry(_environ(OTEL_RESOURCE_ATTRIBUTES=f"infra.iac.ref={IAC_REF}"))
    assert IAC_REF not in str(failure.value)
def test_each_signal_goes_to_the_shared_collector_over_otlp_http() -> None:
    providers = init_telemetry(_environ(), set_global=False); assert providers is not None
    try: assert [sink.endpoint for sink in _SpanSink.created] == [f"{ENDPOINT}/v1/traces"]; assert [sink.endpoint for sink in _MetricSink.created] == [f"{ENDPOINT}/v1/metrics"]; assert [sink.endpoint for sink in _LogSink.created] == [f"{ENDPOINT}/v1/logs"]
    finally: providers.shutdown()
def test_spans_metrics_and_logs_carry_the_rendered_identity() -> None:
    providers = init_telemetry(_environ(), set_global=False); assert providers is not None; expected = {**IDENTITY, "service.name": "truealpha-app"}; logger = logging.getLogger("truealpha.test.telemetry"); handler = LoggingHandler(level=logging.NOTSET, logger_provider=providers.logger_provider); logger.addHandler(handler); logger.propagate = False
    try:
        with providers.tracer_provider.get_tracer("t").start_as_current_span("GET /health"): pass
        providers.meter_provider.get_meter("t").create_counter("requests").add(1); logger.error("boom"); providers.tracer_provider.force_flush(); providers.logger_provider.force_flush(); providers.meter_provider.force_flush()
    finally: logger.removeHandler(handler); providers.shutdown()
    spans = _SpanSink.created[0].get_finished_spans(); assert [span.name for span in spans] == ["GET /health"]; records = _LogSink.created[0].get_finished_logs(); assert [record.log_record.body for record in records] == ["boom"]; metric_batches = _MetricSink.created[0].batches; assert metric_batches, "the counter was never exported"; resources = { "span": spans[0].resource.attributes, "log": records[0].resource.attributes, "metric": metric_batches[-1].resource_metrics[0].resource.attributes, }
    for signal, attributes in resources.items():
        for key, value in expected.items(): assert attributes.get(key) == value, f"{signal}: {key} is {attributes.get(key)!r}, expected {value!r}"
def test_the_trace_sampler_follows_the_standard_environment_variable() -> None:
    def exported(**overrides: str | None) -> int:
        providers = init_telemetry(_environ(**overrides), set_global=False); assert providers is not None
        with providers.tracer_provider.get_tracer("t").start_as_current_span("s"): pass
        providers.tracer_provider.force_flush(); count = len(_SpanSink.created[-1].get_finished_spans()); providers.shutdown(); return count
    assert exported() == 1; assert exported(OTEL_TRACES_SAMPLER="always_off") == 0
    with pytest.raises(TelemetryConfigError, match="OTEL_TRACES_SAMPLER"): init_telemetry(_environ(OTEL_TRACES_SAMPLER="sometimes"))
