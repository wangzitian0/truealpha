"""OTLP telemetry for every Python service, through the released infra2-sdk (#1034).

infra2 runs one shared SigNoz (`platform-signoz-otel-collector:4318`, OTLP/HTTP, reachable only
on the Docker network) and tells environments apart by resource identity, not by collector.
This module is the application half of that contract (infra2 `ops.observability` section 4.2):

* **Off unless an endpoint is configured.** Telemetry runs only when
  `OTEL_EXPORTER_OTLP_ENDPOINT` is set (and `OTEL_SDK_DISABLED` is not `true`). A laptop, CI and
  every test have no endpoint, so nothing is built, nothing is exported and no thread starts.
* **Fail fast once it is on.** The identity is rendered by infra2's deploy, never invented here:
  `OTEL_SERVICE_NAME` plus the four `OTEL_RESOURCE_ATTRIBUTES` entries in
  `REQUIRED_IDENTITY_ATTRIBUTES`. A deployment that turns telemetry on and omits one of them
  gets a refused boot that names the missing keys (names only), not rows that land in SigNoz with
  no `deployment.environment.name` and are invisible to every alert filtered on it.
* **The telemetry tier is the issued name, not `APP_ENV`.** `APP_ENV` is the app's behavioural tier
  (a preview stack runs with `APP_ENV=staging`); `deployment.environment.name` (`production`,
  `staging`, `pr-12`, `branch-main`, `canary-preview`, ...) is what infra2 issues as the telemetry
  identity. They are deliberately not reconciled here.
* **No exporter code of our own.** `infra2_sdk.runtime.otel.configure_telemetry` builds the trace,
  metric and log exporters and installs them as the process globals plus a logging handler, so
  an `ERROR` log record reaches SigNoz Logs with the same resource as the traces.

Each service calls `init_telemetry()` once at startup: `llm_service.main` (FastAPI) and
`data_engine.dagster_defs` (Dagster code server and run workers). The browser/Next.js app is not
a Python service and is not covered here.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from infra2_sdk.runtime.otel import (
    REQUIRED_IDENTITY_ATTRIBUTES,  # noqa: F401  (re-exported: the identity keys the SDK requires)
    OtelSettings,
    TelemetryProviders,
    configure_telemetry,
)


class TelemetryConfigError(RuntimeError):
    """Telemetry is switched on and its configuration is incomplete or contradictory."""


def load_telemetry_settings(environ: Mapping[str, str] | None = None) -> OtelSettings | None:
    """The settings to export with, `None` when telemetry is off, or `TelemetryConfigError`.

    Off means no `OTEL_EXPORTER_OTLP_ENDPOINT`, or `OTEL_SDK_DISABLED=true`. Everything else is
    on, and on is strict: a missing service name or identity attribute, a malformed
    `OTEL_RESOURCE_ATTRIBUTES`, an invalid sampler or `OTEL_SDK_DISABLED`, or an issued
    `deployment.environment.name` the SDK cannot classify is an error rather than a default.
    `APP_ENV` takes no part in it: the SDK reads the telemetry tier from the issued name when
    `require_issued_identity=True`.
    """
    values = os.environ if environ is None else environ
    try:
        settings = OtelSettings.from_env(values, strict=True, require_issued_identity=True)
    except ValueError as exc:
        raise TelemetryConfigError(f"telemetry is enabled but its configuration is invalid: {exc}") from exc
    return settings if settings.enabled else None


def init_telemetry(environ: Mapping[str, str] | None = None, *, set_global: bool = True) -> TelemetryProviders | None:
    """Configure OTLP traces, metrics and logs for this process; `None` when telemetry is off.

    Idempotent per process (the SDK returns the active installation on a second call), so a
    module that two entrypoints both import cannot attach a second logging handler.
    `set_global=False` builds the providers without touching process globals; it exists for tests.
    """
    settings = load_telemetry_settings(environ)
    if settings is None:
        return None
    return configure_telemetry(settings, set_global=set_global)
