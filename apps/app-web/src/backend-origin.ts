// The one resolver of the llm-service origin. The middleware and the server-side API client use it.
// LLM_SERVICE_URL sets the origin: infra2 sets it for every deployed web container (infra2#1907),
// and docker-compose.yml sets it for local runs. Without it, local development uses LOCAL_ORIGIN.
const LOCAL_ORIGIN = "http://127.0.0.1:8000";

export function resolveBackendOrigin(env: Readonly<Record<string, string | undefined>> = process.env): string {
  const configured = env.LLM_SERVICE_URL?.trim();
  return configured ? configured.replace(/\/+$/, "") : LOCAL_ORIGIN;
}
