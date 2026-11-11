"""OTLP telemetry for Python services via infra2-sdk (#1034)."""

from __future__ import annotations

import os
import time
from collections.abc import Iterable, Mapping

from infra2_sdk.runtime.otel import (
    REQUIRED_IDENTITY_ATTRIBUTES,  # noqa: F401
    OtelSettings,
    TelemetryProviders,
    configure_telemetry,
)
from opentelemetry.metrics import CallbackOptions, Observation


class TelemetryConfigError(RuntimeError):
    """Telemetry is enabled and its configuration is incomplete or contradictory."""


def load_telemetry_settings(environ: Mapping[str, str] | None = None) -> OtelSettings | None:
    values = os.environ if environ is None else environ
    try:
        settings = OtelSettings.from_env(values, strict=True, require_issued_identity=True)
    except ValueError as exc:
        raise TelemetryConfigError(
            f"telemetry is enabled (OTEL_EXPORTER_OTLP_ENDPOINT is set) but its configuration is invalid: {exc} "
            f"-- names only; infra2's deploy renders OTEL_SERVICE_NAME and these keys of OTEL_RESOURCE_ATTRIBUTES"
        ) from exc
    return settings if settings.enabled else None


def _register_heartbeat(providers: TelemetryProviders) -> None:
    if providers.meter_provider is None:
        return
    started = time.monotonic()

    def observe(_options: CallbackOptions) -> Iterable[Observation]:
        yield Observation(time.monotonic() - started)

    providers.meter_provider.get_meter("truealpha_runtime").create_observable_gauge(
        "process.uptime",
        callbacks=[observe],
        unit="s",
        description="Seconds since this process started telemetry; present whenever the process exports.",
    )


def init_telemetry(environ: Mapping[str, str] | None = None, *, set_global: bool = True) -> TelemetryProviders | None:
    settings = load_telemetry_settings(environ)
    if settings is None:
        return None
    providers = configure_telemetry(settings, set_global=set_global)
    _register_heartbeat(providers)
    return providers
