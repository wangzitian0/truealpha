/**
 * The walk's navigation primitive. `e2e/walk-tree.mjs` launches a browser when
 * it is imported, so a test cannot import a function from it. This module has
 * no side effects at import.
 */

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
 * region to exist (the assertions below read rendered DOM, so `domcontentloaded` alone is
 * not enough for an app-router page), and retry ONCE. A real outage fails both attempts and
 * still fails fast; a saturation blip no longer costs a deploy.
 */
export async function gotoStable(page, url) {
  let lastError;
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      const response = await page.goto(url, { waitUntil: "domcontentloaded", timeout: 30000 });
      await page.waitForSelector("main, [role='main']", { state: "attached", timeout: 20000 });
      const signOutCount = await page.locator("[data-sign-out]").count();
      if (signOutCount > 0) {
        await page
          .waitForSelector('[data-sign-out][data-hydrated="true"]', { state: "attached", timeout: 15000 })
          .catch(() => null);
      }
      // The response of the navigation that succeeded, for a caller that checks the HTTP status (#1241).
      return response;
    } catch (error) {
      lastError = error;
    }
  }
  throw lastError;
}
