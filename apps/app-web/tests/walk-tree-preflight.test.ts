/**
 * #1875: verify preflightIdentities retries bounded transport drops without
 * retrying HTTP auth/rate-limit answers or leaking BrowserContexts on failure.
 */

import http from "node:http";
import type { AddressInfo } from "node:net";
import { request as playwrightRequest } from "playwright";
import { preflightIdentities } from "../e2e/preflight.mjs";

const bunTestModuleId = "bun:test";
const { test, expect } = (await import(bunTestModuleId)) as {
  test: (name: string, fn: () => Promise<void> | void) => void;
  expect: (value: unknown) => {
    toBe: (expected: unknown) => void;
    toEqual: (expected: unknown) => void;
    toContain: (expected: string) => void;
    rejects: { toThrow: (pattern: RegExp) => Promise<void> };
  };
};

interface TestBrowserHarness {
  opened: number;
  closed: number;
  browser: {
    newContext: () => Promise<{
      request: Awaited<ReturnType<typeof playwrightRequest.newContext>>;
      storageState: () => ReturnType<Awaited<ReturnType<typeof playwrightRequest.newContext>>["storageState"]>;
      close: () => Promise<void>;
    }>;
  };
}

function createHarness(): TestBrowserHarness {
  const harness: TestBrowserHarness = {
    opened: 0,
    closed: 0,
    browser: {
      newContext: async () => {
        const apiContext = await playwrightRequest.newContext();
        harness.opened += 1;
        let isClosed = false;
        return {
          request: apiContext,
          storageState: () => apiContext.storageState(),
          close: async () => {
            if (isClosed) return;
            isClosed = true;
            harness.closed += 1;
            await apiContext.dispose();
          },
        };
      },
    },
  };
  return harness;
}

async function withLoginServer(
  handler: (payload: { email: string; password: string }, req: http.IncomingMessage, res: http.ServerResponse) => void,
  fn: (baseUrl: string) => Promise<void>,
): Promise<void> {
  const server = http.createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", (chunk: Buffer) => chunks.push(chunk));
    req.on("end", () => {
      const parsed = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}") as { email: string; password: string };
      handler(parsed, req, res);
    });
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address() as AddressInfo;
  try {
    await fn(`http://127.0.0.1:${address.port}`);
  } finally {
    server.closeAllConnections();
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
}

const IDENTITIES = [
  { role: "administrator", email: "admin@example.test", password: "pw" },
  { role: "member", email: "member@example.test", password: "pw" },
];

test("retries a dropped socket once and returns usable authenticated contexts", async () => {
  const hits: Record<string, number> = {};
  const receivedEmails: string[] = [];
  const logs: string[] = [];
  const harness = createHarness();

  await withLoginServer(
    ({ email }, req, res) => {
      if (req.url === "/api/v1/auth/session") {
        res.writeHead(200, { "content-type": "application/json" });
        res.end(JSON.stringify({ authenticated: true }));
        return;
      }
      hits[email] = (hits[email] ?? 0) + 1;
      if (email === "admin@example.test" && hits[email] === 1) {
        req.socket.destroy();
        return;
      }
      receivedEmails.push(email);
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ ok: true }));
    },
    async (base) => {
      const sessions = await preflightIdentities(harness.browser, IDENTITIES, {
        base,
        timeouts: { requestMs: 500, maxAttempts: 2 },
        log: (line) => logs.push(line),
      });
      try {
        expect(sessions.length).toBe(2);
        expect(sessions.map((s) => ({ role: s.role, status: s.status }))).toEqual([
          { role: "administrator", status: 200 },
          { role: "member", status: 200 },
        ]);
        expect(hits["admin@example.test"]).toBe(2);
        expect(hits["member@example.test"]).toBe(1);
        expect(receivedEmails).toEqual(["admin@example.test", "member@example.test"]);
        expect(logs.length).toBe(1);
        expect(logs[0]).toContain("retry POST /api/v1/auth/login (administrator): attempt 1 failed:");
        const checkResp = await sessions[0].context.request.get(`${base}/api/v1/auth/session`);
        expect(checkResp.status()).toBe(200);
      } finally {
        await Promise.all(sessions.map((s) => s.context.close()));
      }
    },
  );
  expect(harness.opened).toBe(2);
  expect(harness.closed).toBe(2);
});

test("does not retry HTTP 401 and closes all already-opened contexts", async () => {
  const hits: Record<string, number> = {};
  const errorLogs: string[] = [];
  const harness = createHarness();
  let failedSummary: Array<{ role: string; status: number }> | null = null;

  await withLoginServer(
    ({ email }, _req, res) => {
      hits[email] = (hits[email] ?? 0) + 1;
      if (email === "member@example.test") {
        res.writeHead(401, { "content-type": "application/json" });
        res.end(JSON.stringify({ detail: "invalid_credentials" }));
        return;
      }
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ ok: true }));
    },
    async (base) => {
      const sessions = await preflightIdentities(harness.browser, IDENTITIES, {
        base,
        timeouts: { requestMs: 500, maxAttempts: 2 },
        errorLog: (line) => errorLogs.push(line),
        onAuthFailure: (failed) => {
          failedSummary = failed;
        },
      });
      expect(sessions).toEqual([]);
    },
  );

  expect(hits["member@example.test"]).toBe(1);
  expect(failedSummary).toEqual([{ role: "member", status: 401 }]);
  expect(errorLogs).toEqual(["login failed for member: 401"]);
  expect(harness.opened).toBe(2);
  expect(harness.closed).toBe(2);
});

test("closes earlier opened contexts when a later login exhausts request timeouts", async () => {
  const hits: Record<string, number> = {};
  const logs: string[] = [];
  const harness = createHarness();

  await withLoginServer(
    ({ email }, _req, res) => {
      hits[email] = (hits[email] ?? 0) + 1;
      if (email === "member@example.test") {
        // Intentionally never answer so Playwright hits requestMs on each attempt.
        return;
      }
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ ok: true }));
    },
    async (base) => {
      await expect(
        preflightIdentities(harness.browser, IDENTITIES, {
          base,
          timeouts: { requestMs: 120, maxAttempts: 2 },
          log: (line) => logs.push(line),
        }),
      ).rejects.toThrow(/POST \/api\/v1\/auth\/login \(member\) failed all 2 attempts/);
    },
  );

  expect(hits["admin@example.test"]).toBe(1);
  expect(hits["member@example.test"]).toBe(2);
  expect(logs.length).toBe(1);
  expect(logs[0]).toContain("retry POST /api/v1/auth/login (member): attempt 1 failed:");
  expect(harness.opened).toBe(2);
  expect(harness.closed).toBe(2);
});
