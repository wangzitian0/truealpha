"""OTLP telemetry for Python services via infra2-sdk (#1034)."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

from infra2_sdk.runtime.otel import (
    REQUIRED_IDENTITY_ATTRIBUTES,  # noqa: F401
    TelemetryProviders,
    init_worker,
)


class TelemetryConfigError(RuntimeError):
    """Telemetry is enabled and its configuration is incomplete or contradictory."""


def telemetry_config_error(exc: ValueError) -> TelemetryConfigError:
    return TelemetryConfigError(
        f"telemetry is enabled (OTEL_EXPORTER_OTLP_ENDPOINT is set) but its configuration is invalid: {exc} "
        f"-- names only; infra2's deploy renders OTEL_SERVICE_NAME and these keys of OTEL_RESOURCE_ATTRIBUTES"
    )


def init_telemetry(environ: Mapping[str, str] | None = None, *, set_global: bool = True) -> TelemetryProviders | None:
    values = os.environ if environ is None else environ
    try:
        return init_worker(
            "truealpha",
            strict=True,
            require_issued_identity=True,
            log_level=logging.INFO if set_global else None,
            set_global=set_global,
            environ=values,
        )
    except ValueError as exc:
        raise telemetry_config_error(exc) from exc
