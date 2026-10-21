/**
 * The hydration-race check of the CI walk (#1468).
 *
 * The deployed walk raised React error #418 on 2 to 5 routes per run. The CI walk on a loopback
 * server never did. A loopback server sends all flight rows in one burst. The deployed server sends
 * the last row (`AppChrome`) later, so the browser can start to hydrate while a row is missing.
 * The React copy in Next 15.5 then took a second server node (see late-row-proxy.mjs).
 *
 * This check puts the late-row proxy in front of the server and visits routes many times.
 * It fails on any page error, on a retry of the navigation, and on a visit that the proxy did not hold.
 * Measured on Next 15.5.18, one visit in about four failed: 5 to 7 of 24 visits in each of 6 runs.
 * So all 24 visits pass with a chance below 1 in 200. On Next 16.4.2, 0 of 144 visits failed.
 * This module has no side effects at import. `tests/hydration-race.test.ts` drives it.
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
 *   navigate?: (page: any, url: string, options: { log: (line: string) => void }) => Promise<unknown>,
 * }} options `navigate` is `gotoStable`. A test replaces it.
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
  navigate = gotoStable,
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
            // A retry means that the first navigation did not hydrate. It is a failure, not a pass.
            await navigate(page, `${proxy.url}${route}`, { log: (line) => errors.push(`navigation retried: ${line.trim()}`) });
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
    // A proxy that held nothing back tests nothing (GREEN-WHILE-EMPTY). Count per route.
    for (const route of routes) {
      const held = proxy.stats.held.filter((path) => path === route).length;
      if (held < visitsPerRoute) {
        problems.push(
          `[hydration-race] ${route}: the proxy held a late flight row for ${held} of ${visitsPerRoute} visits. ` +
            (held === 0
              ? "No document of this route has a lazy child under a host element, or the row parser no longer reads the flight format. " +
                "If the first is true, the cause of #1468 is gone: delete this check."
              : "Some visits reached the server without the proxy."),
        );
      }
    }
    return { problems, visits: visitList.length, held: proxy.stats.held.length };
  } finally {
    await context.close();
    await proxy.close();
  }
}
