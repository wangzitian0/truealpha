import { getBackendBaseUrl } from "../src/client/api";
import { resolveBackendOrigin } from "../src/backend-origin";

function assertEqual(actual: string, expected: string, message: string): void {
  if (actual !== expected) throw new Error(`${message}: got ${actual}, expected ${expected}`);
}

// The middleware and the API client share this one resolver.
assertEqual(
  resolveBackendOrigin({ LLM_SERVICE_URL: " http://llm.example:9000// ", ENVIRONMENT: "staging" }),
  "http://llm.example:9000",
  "LLM_SERVICE_URL sets the origin and loses its trailing slashes",
);
for (const env of [{ ENV_SUFFIX: "-pr-12" }, { ENVIRONMENT: "staging" }, { ENVIRONMENT: "production" }, { APP_ENV: "staging" }]) {
  assertEqual(
    resolveBackendOrigin(env),
    "http://127.0.0.1:8000",
    `without LLM_SERVICE_URL no fixed host is derived from ${JSON.stringify(env)}`,
  );
}

const saved = process.env.LLM_SERVICE_URL;
try {
  process.env.LLM_SERVICE_URL = "http://from-process-env:8000";
  assertEqual(getBackendBaseUrl(), "http://from-process-env:8000", "the API client must read process.env through the resolver");
} finally {
  if (saved === undefined) delete process.env.LLM_SERVICE_URL;
  else process.env.LLM_SERVICE_URL = saved;
}

console.log("backend-origin-env passed");
