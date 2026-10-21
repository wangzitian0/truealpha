/**
 * The hydration-race check of the CI walk (#1468).
 *
 * The deployed walk raised React error #418 on 2 to 5 routes per run, and the CI walk on a loopback
 * server never did. A loopback server delivers all flight rows in one burst. The deployed server
 * delivers the last row (`AppChrome`) later, so the browser can start to hydrate while a row is
 * missing. The React copy in Next 15.5 then claimed a server node twice (see late-row-proxy.mjs).
 *
 * This check puts the late-row proxy in front of the server, visits routes many times, and fails
 * on any page error. One visit on Next 15.5.18 failed with a chance of about one in five (measured:
 * 9 of 40 visits). With 24 visits the chance that all pass is below 1 in 300. On Next 16.4.2 the
 * measured count is 0 of 110 visits. This module has no side effects at import.
 */

import { startLateRowProxy } from "./late-row-proxy.mjs";
import { gotoStable } from "./navigation.mjs";

/** Routes whose pages hold lazy rows in the seeded fixture: a header row and table rows. */
export const RACE_ROUTES = Object.freeze([
  "/research/backtest",
  "/research/rankings",
  "/research/themes",
  "/research/strategy",
]);

export const RACE_VISITS_PER_ROUTE = 6;
export const RACE_HOLD_MS = 400;
const PARALLEL_VISITS = 4;

const firstLine = (error) => String(error?.message ?? error).split("\n")[0].slice(0, 200);

/**
 * @param {{
 *   browser: any,
 *   base: string,
 *   email: string,
 *   password: string,
 *   routes?: readonly string[],
 *   visitsPerRoute?: number,
 *   holdMs?: number,
 * }} options
 * @returns {Promise<{ problems: string[], visits: number, held: number }>}
 */
export async function runHydrationRace({
  browser,
  base,
  email,
  password,
  routes = RACE_ROUTES,
  visitsPerRoute = RACE_VISITS_PER_ROUTE,
  holdMs = RACE_HOLD_MS,
}) {
  const proxy = await startLateRowProxy({ origin: base, holdMs });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const problems = [];
  try {
    const login = await context.request.post(`${proxy.url}/api/auth/login`, { data: { email, password } });
    if (login.status() !== 200) {
      return { problems: [`[hydration-race] login answered HTTP ${login.status()}`], visits: 0, held: 0 };
    }
    const visitList = routes.flatMap((route) => Array.from({ length: visitsPerRoute }, (_, index) => ({ route, index })));
    for (let from = 0; from < visitList.length; from += PARALLEL_VISITS) {
      await Promise.all(
        visitList.slice(from, from + PARALLEL_VISITS).map(async ({ route, index }) => {
          const page = await context.newPage();
          const errors = [];
          page.on("pageerror", (error) => errors.push(firstLine(error)));
          try {
            await gotoStable(page, `${proxy.url}${route}`, { log: () => {} });
            // A recoverable hydration error is reported after the first commit.
            await page.waitForTimeout(300);
          } catch (error) {
            errors.push(`did not hydrate: ${firstLine(error)}`);
          } finally {
            await page.close();
          }
          for (const message of new Set(errors)) problems.push(`[hydration-race] ${route} visit ${index + 1}: ${message}`);
        }),
      );
    }
    const visits = visitList.length;
    // A proxy that held nothing back tests nothing (GREEN-WHILE-EMPTY).
    if (proxy.stats.held < visits || proxy.stats.unheld.length > 0) {
      problems.push(
        `[hydration-race] the proxy held a late flight row for ${proxy.stats.held} of ${visits} documents; ` +
          `documents without a late row: ${proxy.stats.unheld.join(", ") || "none"}. ` +
          "If no page has a late row any more, the cause of #1468 is gone: delete this check.",
      );
    }
    return { problems, visits, held: proxy.stats.held };
  } finally {
    await context.close();
    await proxy.close();
  }
}
