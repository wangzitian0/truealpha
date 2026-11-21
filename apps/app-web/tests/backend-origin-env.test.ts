import { getBackendBaseUrl } from "../src/client/api";
import { resolveBackendOrigin } from "../src/backend-origin";

function assertEqual(actual: string, expected: string, message: string): void {
  if (actual !== expected) throw new Error(`${message}: got ${actual}, expected ${expected}`);
}

// The middleware and the API client share this one resolver.
assertEqual(
  resolveBackendOrigin({ LLM_SERVICE_URL: " http://llm.example:9000// ", ENVIRONMENT: "staging" }),
  "http://llm.example:9000",
  "LLM_SERVICE_URL must win over every fallback and lose its trailing slashes",
);
assertEqual(
  resolveBackendOrigin({ ENV_SUFFIX: "-pr-12", ENVIRONMENT: "staging" }),
  "http://truealpha-llm-pr-12:8000",
  "ENV_SUFFIX must name the llm container before ENVIRONMENT does",
);
assertEqual(
  resolveBackendOrigin({ ENVIRONMENT: "staging" }),
  "http://truealpha-llm-staging:8000",
  "ENVIRONMENT=staging must route to the staging llm container",
);
assertEqual(
  resolveBackendOrigin({ ENVIRONMENT: "production" }),
  "http://truealpha-llm:8000",
  "ENVIRONMENT=production must route to the production llm container",
);
assertEqual(
  resolveBackendOrigin({ APP_ENV: "staging" }),
  "http://127.0.0.1:8000",
  "retired APP_ENV alias must not act as a fallback for ENVIRONMENT",
);

const saved = process.env.LLM_SERVICE_URL;
try {
  process.env.LLM_SERVICE_URL = "http://from-process-env:8000";
  assertEqual(getBackendBaseUrl(), "http://from-process-env:8000", "the API client must read process.env through the resolver");
} finally {
  if (saved === undefined) delete process.env.LLM_SERVICE_URL;
  else process.env.LLM_SERVICE_URL = saved;
}

console.log("backend-origin-env passed");
