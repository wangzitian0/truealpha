/**
 * The walk's navigation primitive and its page-error ledger.
 *
 * `e2e/walk-tree.mjs` launches a browser when it is imported, so a test cannot
 * import a function from it. This module has no side effects at import.
 * `tests/walk-tree-hydration.test.ts` drives both exports against a fake page.
 */

/**
 * Set by `HydrationMarker` (src/components/hydration-marker.tsx) in an effect.
 * The root layout renders it, so every page sets the attribute once React has
 * hydrated the page. The server HTML never carries it.
 * `tests/hydration-marker.test.ts` checks both halves.
 */
export const HYDRATED_DOCUMENT = 'html[data-hydrated="true"]';

/** @typedef {{ gotoMs: number, mainMs: number, hydrationMs: number }} NavigationTimeouts */

/** @type {Readonly<NavigationTimeouts>} */
export const NAVIGATION_TIMEOUTS = Object.freeze({
  gotoMs: 30_000,
  mainMs: 20_000,
  // Bounds the wait for the client bundle to download, parse and hydrate the
  // page. It costs time only when hydration is slow or broken. No walk log
  // measures hydration directly. What the logs do show: a passing sign-out
  // journey takes 3-8 s end to end (v0.0.70-v0.0.73), and both pre-#853 misses
  // spent the whole 15 s window without reaching /login. The walk opens its
  // first page about a minute after a container swap on a shared host, so the
  // bound is three of those windows. Moved from sign-out-journey.mjs (#1412),
  // which now uses this value.
  hydrationMs: 45_000,
});

const seconds = (ms) => `${ms / 1000} s`;
const firstLine = (error) => String(error?.message ?? error).split("\n")[0];

/** The path and query of a URL: the route that a failure names. */
function routeOf(url) {
  const parsed = new URL(url);
  return `${parsed.pathname}${parsed.search}`;
}

/** Navigate deterministically, and survive a saturated host without hiding a real outage.
 *
 * `waitUntil: "networkidle"` was the previous wait everywhere here. Playwright's own docs
 * mark it DISCOURAGED, and on 2026-08-31 it failed a production deploy that was entirely
 * healthy: every container had been up 21 hours, `https://truealpha.club/login` answered
 * 200 in 0.38s, and the walk still timed out after 30s. The host runs 93 containers across
 * four projects at load ~8.5 on 8 cores, so "500ms with no network activity" is a condition
 * that may simply never occur -- the gate was measuring the neighbours' traffic.
 *
 * A gate that fails while the service is fine is worse than no gate: it trains everyone to
 * re-run it. So: wait for a deterministic lifecycle event, then for the page's own main
 * region to exist (the assertions read rendered DOM, so `domcontentloaded` alone is not
 * enough for an app-router page), then for the page to hydrate, and retry ONCE. A real
 * outage fails both attempts and still fails fast; a saturation blip no longer costs a
 * deploy. The walk prints a failed first attempt, so a retry is never silent.
 *
 * #1412 W2: the hydration wait applies to every page and has no catch. Before, the walk
 * waited only when a sign-out control was on the page, and a timeout of that wait was
 * dropped. So the walk could read, leave or click a page before React had hydrated it,
 * and a hydration error of one page could reach the verdict of the next route. Every walk
 * navigation goes through this function (`tests/walk-tree-hydration.test.ts` fails on a
 * direct `goto()`), so every page that the walk leaves has hydrated. When the page never
 * hydrates, the walk stops with an error that names the route and the HTTP status.
 *
 * @param {any} page a Playwright page
 * @param {string} url
 * @param {{ timeouts?: Readonly<NavigationTimeouts>, log?: (line: string) => void }} [options]
 */
export async function gotoStable(page, url, { timeouts = NAVIGATION_TIMEOUTS, log = console.log } = {}) {
  const route = routeOf(url);
  const failed = [];
  for (let attempt = 1; attempt <= 2; attempt += 1) {
    let status = "no response";
    try {
      const response = await page.goto(url, { waitUntil: "domcontentloaded", timeout: timeouts.gotoMs });
      if (response) status = `HTTP ${response.status()}`;
      await page.waitForSelector("main, [role='main']", { state: "attached", timeout: timeouts.mainMs });
      try {
        await page.waitForSelector(HYDRATED_DOCUMENT, { state: "attached", timeout: timeouts.hydrationMs });
      } catch {
        throw new Error(`${route} answered ${status} and did not hydrate: no ${HYDRATED_DOCUMENT} within ${seconds(timeouts.hydrationMs)}`);
      }
      // The response of the navigation that succeeded, for a caller that checks the HTTP status (#1241).
      return response;
    } catch (error) {
      failed.push(`attempt ${attempt}: ${firstLine(error)}`);
      if (attempt === 1) log(`  retry ${route}: attempt 1 failed: ${firstLine(error)}`);
    }
  }
  throw new Error(`gotoStable: ${route} failed twice. ${failed.join("; ")}`);
}

/**
 * Collects the page errors of a walk. Every page error fails a route.
 *
 * React error #418 ("Hydration failed because the server rendered HTML didn't match the client")
 * is a page error like any other. The walk ignored it from 8711285a until #1468. An ignored #418
 * hides a real defect: React throws away the server markup and renders the page again on the client.
 * The race check (hydration-race.mjs) shows #418 on Next 15.5.18 and none on Next 16.4.2.
 *
 * A page error can arrive after the walk read the verdict of a route. The ledger keeps it in
 * `unread`. The next route reports it, and `lateProblems()` reports it at the end of a pass.
 */
export function createPageErrorLedger() {
  /** @type {string | null} */
  let currentPath = null;
  /** The page errors of the current route. */
  /** @type {string[]} */
  const failures = [];
  /** The page errors that no verdict has reported yet. */
  /** @type {string[]} */
  let unread = [];
  return {
    failures,
    /** @returns {string[]} the problems that the page errors of the current route add to its verdict */
    problems() {
      unread = [];
      return failures.map((error) => `pageerror: ${error}`);
    },
    /** @returns {string[]} the problems of page errors that came after the last verdict */
    lateProblems() {
      const late = unread.map((error) => `pageerror after the last route: ${error}`);
      unread = [];
      return late;
    },
    /**
     * @param {string} _role
     * @param {string} path
     */
    startRoute(_role, path) {
      currentPath = path;
      failures.length = 0;
      failures.push(...unread);
    },
    /**
     * @param {string} message
     * @param {string} pagePath the path of the page that raised it
     */
    record(message, pagePath) {
      // assertTraceRendersDecisionInputs opens /research first, so an error can come from another page.
      const text = currentPath !== null && pagePath !== currentPath ? `${message} (raised on ${pagePath})` : message;
      failures.push(text);
      unread.push(text);
    },
  };
}
