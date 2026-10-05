"""truealpha_runtime.telemetry (#1034): off without an endpoint, fail-fast with one, exact identity.

No network anywhere. The three OTLP exporter classes the SDK constructs are replaced by in-memory
ones for the whole module, so what is asserted is what infra2's SigNoz would receive: the endpoint
each signal is sent to and the resource identity every span, metric point and log record carries.
Every provider is built with `set_global=False`, so no test installs a process-wide provider or a
root-logger handler; the entrypoints that do (`llm_service.main`, `data_engine.dagster_defs`) are
asserted in their own packages, in a subprocess.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry.exporter.otlp.proto.http import _log_exporter, metric_exporter, trace_exporter
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from truealpha_runtime.telemetry import (
    REQUIRED_IDENTITY_ATTRIBUTES,
    TelemetryConfigError,
    init_telemetry,
    load_telemetry_settings,
)

ENDPOINT = "http://platform-signoz-otel-collector:4318"
IAC_REF = "0123456789abcdef0123456789abcdef01234567"
IDENTITY = {
    "deployment.environment.name": "staging",
    "infra.service.id": "truealpha/app",
    "service.version": "v1.2.3",
    "infra.iac.ref": IAC_REF,
}


def _environ(**overrides: str | None) -> dict[str, str]:
    """What infra2's deploy renders for one service; `None` removes a variable."""
    environ = {
        "OTEL_EXPORTER_OTLP_ENDPOINT": ENDPOINT,
        "OTEL_SERVICE_NAME": "truealpha-app",
        "APP_ENV": "staging",
        "OTEL_RESOURCE_ATTRIBUTES": ",".join(f"{key}={value}" for key, value in IDENTITY.items()),
    }
    for name, value in overrides.items():
        if value is None:
            environ.pop(name)
        else:
            environ[name] = value
    return environ


class _SpanSink(InMemorySpanExporter):
    created: list[_SpanSink] = []

    def __init__(self, endpoint: str | None = None, **_: Any) -> None:
        super().__init__()
        self.endpoint = endpoint
        _SpanSink.created.append(self)


class _LogSink(InMemoryLogRecordExporter):
    created: list[_LogSink] = []

    def __init__(self, endpoint: str | None = None, **_: Any) -> None:
        super().__init__()
        self.endpoint = endpoint
        _LogSink.created.append(self)


class _MetricSink(MetricExporter):
    created: list[_MetricSink] = []

    def __init__(self, endpoint: str | None = None, **_: Any) -> None:
        super().__init__()
        self.endpoint = endpoint
        self.batches: list[Any] = []
        _MetricSink.created.append(self)

    def export(self, metrics_data: Any, timeout_millis: float = 10_000, **_: Any) -> MetricExportResult:
        self.batches.append(metrics_data)
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return True

    def shutdown(self, timeout_millis: float = 30_000, **_: Any) -> None:
        return None


@pytest.fixture(autouse=True)
def sinks(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The SDK looks the exporter classes up on their modules when it builds providers."""
    for sink in (_SpanSink, _LogSink, _MetricSink):
        sink.created = []
    monkeypatch.setattr(trace_exporter, "OTLPSpanExporter", _SpanSink)
    monkeypatch.setattr(metric_exporter, "OTLPMetricExporter", _MetricSink)
    monkeypatch.setattr(_log_exporter, "OTLPLogExporter", _LogSink)
    yield


def _exporters_created() -> int:
    return len(_SpanSink.created) + len(_LogSink.created) + len(_MetricSink.created)


# --- off: local development, CI and every test --------------------------------------------


def test_no_endpoint_means_no_telemetry_and_no_exporter() -> None:
    assert load_telemetry_settings({}) is None
    assert init_telemetry({}) is None
    # Identity variables alone do not switch it on: the endpoint is the switch.
    assert init_telemetry(_environ(OTEL_EXPORTER_OTLP_ENDPOINT=None)) is None
    assert _exporters_created() == 0


def test_an_empty_endpoint_is_no_endpoint() -> None:
    """An unrendered template line (`OTEL_EXPORTER_OTLP_ENDPOINT=`) must not enable export."""
    assert init_telemetry(_environ(OTEL_EXPORTER_OTLP_ENDPOINT="  ")) is None
    assert _exporters_created() == 0


def test_the_standard_disable_switch_wins_over_an_endpoint() -> None:
    """`OTEL_SDK_DISABLED=true` is the operator's off-switch, and it needs no identity."""
    bare = {"OTEL_EXPORTER_OTLP_ENDPOINT": ENDPOINT, "OTEL_SDK_DISABLED": "true"}
    assert init_telemetry(bare) is None
    assert init_telemetry(_environ(OTEL_SDK_DISABLED="TRUE")) is None
    assert _exporters_created() == 0


def test_the_process_environment_is_the_default_source(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_SDK_DISABLED",
        "OTEL_SERVICE_NAME",
        "OTEL_RESOURCE_ATTRIBUTES",
    ):
        monkeypatch.delenv(name, raising=False)
    assert init_telemetry() is None
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("APP_ENV", "staging")
    with pytest.raises(TelemetryConfigError, match="OTEL_SERVICE_NAME"):
        init_telemetry()


# --- on without the identity infra2 renders: refuse, by name -------------------------------


@pytest.mark.parametrize("key", REQUIRED_IDENTITY_ATTRIBUTES)
def test_a_missing_identity_attribute_refuses_telemetry_naming_only_that_key(key: str) -> None:
    attributes = ",".join(f"{name}={value}" for name, value in IDENTITY.items() if name != key)
    with pytest.raises(TelemetryConfigError) as failure:
        init_telemetry(_environ(OTEL_RESOURCE_ATTRIBUTES=attributes))
    message = str(failure.value)
    assert key in message
    for other in REQUIRED_IDENTITY_ATTRIBUTES:
        assert other == key or other not in message, f"{other} is present and must not be reported"
    assert _exporters_created() == 0, "a refused configuration must not leave exporters running"


def test_the_identity_that_is_missing_altogether_names_all_four() -> None:
    with pytest.raises(TelemetryConfigError) as failure:
        init_telemetry(_environ(OTEL_RESOURCE_ATTRIBUTES=None))
    assert all(key in str(failure.value) for key in REQUIRED_IDENTITY_ATTRIBUTES)


def test_a_blank_identity_value_counts_as_missing() -> None:
    blank = ",".join(f"{name}={'%20' if name == 'infra.iac.ref' else value}" for name, value in IDENTITY.items())
    with pytest.raises(TelemetryConfigError, match=r"infra\.iac\.ref"):
        init_telemetry(_environ(OTEL_RESOURCE_ATTRIBUTES=blank))


def test_a_missing_service_name_refuses_telemetry() -> None:
    """The SDK would call an unnamed service `unknown_service`; an alert cannot be written for it."""
    with pytest.raises(TelemetryConfigError, match="OTEL_SERVICE_NAME"):
        init_telemetry(_environ(OTEL_SERVICE_NAME=None))
    assert _exporters_created() == 0


def test_the_sdk_service_name_alias_is_accepted() -> None:
    providers = init_telemetry(_environ(OTEL_SERVICE_NAME=None, SERVICE_NAME="truealpha-dagster"), set_global=False)
    assert providers is not None
    try:
        assert providers.tracer_provider.resource.attributes["service.name"] == "truealpha-dagster"
    finally:
        providers.shutdown()


def test_a_legacy_deployment_environment_key_does_not_stand_in_for_the_standard_one() -> None:
    legacy = ",".join(
        f"{'deployment.environment' if name == 'deployment.environment.name' else name}={value}"
        for name, value in IDENTITY.items()
    )
    with pytest.raises(TelemetryConfigError, match=r"deployment\.environment\.name"):
        init_telemetry(_environ(OTEL_RESOURCE_ATTRIBUTES=legacy))


def test_a_deployment_environment_that_contradicts_app_env_is_refused() -> None:
    """Staging telemetry labelled `production` would page the wrong environment."""
    with pytest.raises(TelemetryConfigError, match="disagrees"):
        init_telemetry(_environ(APP_ENV="production"))
    assert _exporters_created() == 0


def test_a_missing_app_env_and_a_malformed_attribute_string_are_refused() -> None:
    with pytest.raises(TelemetryConfigError, match="ENVIRONMENT"):
        init_telemetry(_environ(APP_ENV=None))
    with pytest.raises(TelemetryConfigError, match="key=value"):
        init_telemetry(_environ(OTEL_RESOURCE_ATTRIBUTES="deployment.environment.name"))
    assert _exporters_created() == 0


def test_the_refusal_names_variables_and_never_their_values() -> None:
    with pytest.raises(TelemetryConfigError) as failure:
        init_telemetry(_environ(OTEL_RESOURCE_ATTRIBUTES=f"infra.iac.ref={IAC_REF}"))
    assert IAC_REF not in str(failure.value)


# --- on with the full identity: what SigNoz receives ----------------------------------------


def test_each_signal_goes_to_the_shared_collector_over_otlp_http() -> None:
    providers = init_telemetry(_environ(), set_global=False)
    assert providers is not None
    try:
        assert [sink.endpoint for sink in _SpanSink.created] == [f"{ENDPOINT}/v1/traces"]
        assert [sink.endpoint for sink in _MetricSink.created] == [f"{ENDPOINT}/v1/metrics"]
        assert [sink.endpoint for sink in _LogSink.created] == [f"{ENDPOINT}/v1/logs"]
    finally:
        providers.shutdown()


def test_spans_metrics_and_logs_carry_the_rendered_identity() -> None:
    providers = init_telemetry(_environ(), set_global=False)
    assert providers is not None
    expected = {**IDENTITY, "service.name": "truealpha-app"}

    logger = logging.getLogger("truealpha.test.telemetry")
    handler = LoggingHandler(level=logging.NOTSET, logger_provider=providers.logger_provider)
    logger.addHandler(handler)
    logger.propagate = False
    try:
        with providers.tracer_provider.get_tracer("t").start_as_current_span("GET /health"):
            pass
        providers.meter_provider.get_meter("t").create_counter("requests").add(1)
        logger.error("boom")
        providers.tracer_provider.force_flush()
        providers.logger_provider.force_flush()
        providers.meter_provider.force_flush()
    finally:
        logger.removeHandler(handler)
        providers.shutdown()

    spans = _SpanSink.created[0].get_finished_spans()
    assert [span.name for span in spans] == ["GET /health"]
    records = _LogSink.created[0].get_finished_logs()
    assert [record.log_record.body for record in records] == ["boom"]
    metric_batches = _MetricSink.created[0].batches
    assert metric_batches, "the counter was never exported"

    resources = {
        "span": spans[0].resource.attributes,
        "log": records[0].resource.attributes,
        "metric": metric_batches[-1].resource_metrics[0].resource.attributes,
    }
    for signal, attributes in resources.items():
        for key, value in expected.items():
            assert attributes.get(key) == value, f"{signal}: {key} is {attributes.get(key)!r}, expected {value!r}"


def test_the_trace_sampler_follows_the_standard_environment_variable() -> None:
    def exported(**overrides: str | None) -> int:
        providers = init_telemetry(_environ(**overrides), set_global=False)
        assert providers is not None
        with providers.tracer_provider.get_tracer("t").start_as_current_span("s"):
            pass
        providers.tracer_provider.force_flush()
        count = len(_SpanSink.created[-1].get_finished_spans())
        providers.shutdown()
        return count

    assert exported() == 1
    assert exported(OTEL_TRACES_SAMPLER="always_off") == 0
    with pytest.raises(TelemetryConfigError, match="OTEL_TRACES_SAMPLER"):
        init_telemetry(_environ(OTEL_TRACES_SAMPLER="sometimes"))
