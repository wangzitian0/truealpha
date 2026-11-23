// The one resolver of the llm-service origin. The middleware and the server-side API client use it.
// LLM_SERVICE_URL sets the origin. Without it, the container name comes from ENV_SUFFIX.
// Fallback: the deployed web container gets ENVIRONMENT only (infra2 10.app compose), so ENVIRONMENT
// selects the suffix. Remove that fallback when infra2 sets LLM_SERVICE_URL for web (infra2#1823).
// Removal condition: infra2#1907 runs in production, so production web receives LLM_SERVICE_URL.
const LOCAL_ORIGIN = "http://127.0.0.1:8000";

const SUFFIX_BY_ENVIRONMENT: Readonly<Record<string, string>> = {
  staging: "-staging",
  prod: "",
  production: "",
};

function llmContainerOrigin(suffix: string): string {
  return `http://truealpha-llm${suffix}:8000`;
}

export function resolveBackendOrigin(env: Readonly<Record<string, string | undefined>> = process.env): string {
  const configured = env.LLM_SERVICE_URL?.trim();
  if (configured) return configured.replace(/\/+$/, "");
  if (env.ENV_SUFFIX !== undefined) return llmContainerOrigin(env.ENV_SUFFIX);
  const suffix = SUFFIX_BY_ENVIRONMENT[(env.ENVIRONMENT || "").trim().toLowerCase()];
  return suffix === undefined ? LOCAL_ORIGIN : llmContainerOrigin(suffix);
}
