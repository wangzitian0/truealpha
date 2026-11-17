import { getBackendBaseUrl } from "../src/client/api";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const saved = {
  LLM_SERVICE_URL: process.env.LLM_SERVICE_URL,
  ENV_SUFFIX: process.env.ENV_SUFFIX,
  ENVIRONMENT: process.env.ENVIRONMENT,
  APP_ENV: process.env.APP_ENV,
};

function resetEnv(): void {
  delete process.env.LLM_SERVICE_URL;
  delete process.env.ENV_SUFFIX;
  delete process.env.ENVIRONMENT;
  delete process.env.APP_ENV;
}

try {
  resetEnv();
  process.env.ENVIRONMENT = "staging";
  assert(
    getBackendBaseUrl() === "http://truealpha-llm-staging:8000",
    "ENVIRONMENT=staging must route to truealpha-llm-staging",
  );

  resetEnv();
  process.env.ENVIRONMENT = "production";
  assert(
    getBackendBaseUrl() === "http://truealpha-llm:8000",
    "ENVIRONMENT=production must route to truealpha-llm",
  );

  resetEnv();
  process.env.APP_ENV = "staging";
  assert(
    getBackendBaseUrl() === "http://127.0.0.1:8000",
    "retired APP_ENV alias must not act as a fallback for ENVIRONMENT",
  );
} finally {
  for (const [k, v] of Object.entries(saved)) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
}

console.log("backend-origin-env passed");
