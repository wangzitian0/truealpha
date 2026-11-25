/**
 * #1875: authenticate all walk identities before navigating routes, retrying
 * bounded transport/timeout errors without retrying credential or rate-limit responses.
 */

/** @typedef {{ requestMs: number, maxAttempts: number }} PreflightTimeouts */

/** @type {Readonly<PreflightTimeouts>} */
export const PREFLIGHT_TIMEOUTS = Object.freeze({
  requestMs: 15_000,
  maxAttempts: 2,
});

const VIEWPORT = { width: 1440, height: 900 };
const firstLine = (error) => String(error?.message ?? error).split("\n")[0];

/**
 * Close all created browser contexts without masking an earlier error.
 * @param {Array<{ close: () => Promise<void> }>} contexts
 */
async function closeAll(contexts) {
  await Promise.allSettled(contexts.map((context) => context.close()));
}

/**
 * Verify every identity can authenticate before walking routes so credential
 * drift fails fast while a single dropped edge packet retries once.
 *
 * @param {any} browser
 * @param {Array<{ role: string, email: string, password: string }>} identities
 * @param {{ base: string, timeouts?: Readonly<PreflightTimeouts>, log?: (line: string) => void, errorLog?: (line: string) => void, onAuthFailure?: (failed: Array<{ role: string, status: number }>) => unknown }} options
 */
export async function preflightIdentities(
  browser,
  identities,
  { base, timeouts = PREFLIGHT_TIMEOUTS, log = console.log, errorLog = console.error, onAuthFailure } = {},
) {
  /** @type {Array<{ close: () => Promise<void> }>} */
  const openedContexts = [];
  /** @type {Array<{ role: string, status: number, context: any }>} */
  const sessions = [];

  try {
    for (const { role, email, password } of identities) {
      const context = await browser.newContext({ viewport: VIEWPORT });
      openedContexts.push(context);
      const failed = [];
      let login = null;

      for (let attempt = 1; attempt <= timeouts.maxAttempts; attempt += 1) {
        try {
          login = await context.request.post(`${base}/api/v1/auth/login`, {
            data: { email, password },
            timeout: timeouts.requestMs,
          });
          break;
        } catch (error) {
          failed.push(`attempt ${attempt}: ${firstLine(error)}`);
          if (attempt < timeouts.maxAttempts) {
            log(`  retry POST /api/v1/auth/login (${role}): attempt ${attempt} failed: ${firstLine(error)}`);
          }
        }
      }

      if (!login) {
        throw new Error(
          `POST /api/v1/auth/login (${role}) failed all ${timeouts.maxAttempts} attempts — ${failed.join("; ")}`,
        );
      }
      sessions.push({ role, status: login.status(), context });
    }
  } catch (error) {
    await closeAll(openedContexts);
    throw error;
  }

  const badStatus = sessions.filter((session) => session.status !== 200);
  if (badStatus.length > 0) {
    for (const { role, status } of badStatus) {
      errorLog(`login failed for ${role}: ${status}`);
    }
    await closeAll(openedContexts);
    if (onAuthFailure) {
      onAuthFailure(badStatus.map(({ role, status }) => ({ role, status })));
      return [];
    }
    process.exit(2);
  }

  return sessions;
}
