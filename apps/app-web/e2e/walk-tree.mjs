/**
 * #494 acceptance 1: walk every live route in routes.frozen.json against a
 * running deployment and assert the three contract properties:
 *   (a) research first screens show NO raw entity ids as visible text
 *       (ids may appear only in attributes: title/href/data-evidence);
 *   (b) the claim-ceiling banner is present on rankings + strategy (+ the
 *       overview when selections render);
 *   (c) OPERATE chrome on every /admin page, absent on every /research page.
 *
 * Runs against ANY environment:
 *   TA_BASE_URL=https://truealpha-staging.truealpha.club \
 *   TA_EMAIL=owner@truealpha.club TA_PASSWORD=… \
 *   node e2e/walk-tree.mjs
 *
 * In CI it runs against the workflow's own build over a database seeded by
 * `scripts/seed-e2e-fixture.ts` — see `.github/workflows/ci-web.yml`. That seed
 * is load-bearing: on an empty database every research surface renders
 * `unavailable`, and a property the page never reaches is not being asserted.
 *
 * Uses playwright (chromium) from the environment; prints every checked route
 * and exits non-zero if any assertion failed.
 *
 * ── Structure, for the issues stacked on this one ──
 * Each contract property is one `assert*` function below; `walkRoutes` runs a
 * full pass under one identity. Add a property by adding a function and calling
 * it from `checkRoute`. Add an identity-dependent property (#371's nav
 * membership, #540's sign-out journey) by adding a pass in the driver at the
 * bottom. Append — do not restructure; three issues share this file.
 */

import { readFileSync } from "node:fs";
import { chromium } from "playwright";

const BASE = process.env.TA_BASE_URL;
const EMAIL = process.env.TA_EMAIL;
const PASSWORD = process.env.TA_PASSWORD;
if (!BASE || !EMAIL || !PASSWORD) {
  console.error("TA_BASE_URL, TA_EMAIL, TA_PASSWORD are required");
  process.exit(2);
}

const frozen = JSON.parse(readFileSync(new URL("../routes.frozen.json", import.meta.url), "utf8"));
const livePages = frozen.routes
  .filter((r) => r.live && !r.path.startsWith("/api/") && !r.path.includes("[") && r.path !== "/" && !r.path.startsWith("/admin/api"))
  .map((r) => r.path);

const ID_PATTERN = /(issuer:lei:[A-Z0-9]{10,}|listing:x[a-z]+:[a-z.]+)/;

/**
 * (a) No raw entity id as VISIBLE text on a research first screen. innerText
 * excludes attributes, so title=/href= demotions pass and visible leaks fail.
 *
 * Known limitation on a CI database: this passes vacuously there. Making it a
 * real check needs an LEI-shaped issuer AND its `mart.entity_display_resolution`
 * row — and that view reads `staging.topt_core_snapshot_members`, whose insert
 * trigger requires durable normalized-payload lineage for every observation id.
 * The seeder deliberately does not fabricate that chain. Against staging and
 * production, where real snapshots exist, this is a real check. See #494.
 */
async function assertNoVisibleRawIds(page, path, problems) {
  if (!path.startsWith("/research")) return;
  // innerText must be computed on the LIVE body (layout-aware: excludes
  // script/RSC payloads a detached clone would leak); the page is throwaway,
  // so evidence elements are simply removed first.
  const visible = await page.evaluate(() => {
    for (const el of document.querySelectorAll("[data-evidence]")) el.remove();
    return document.body.innerText;
  });
  const match = visible.match(ID_PATTERN);
  if (match) problems.push(`visible raw id: ${match[0]}`);
}

/**
 * (b) The claim ceiling renders wherever strategy output does. Unconditional on
 * both pages by construction; `tests/claim-ceiling-placement.test.ts` fails at
 * the source file if an early return is reintroduced above either banner.
 */
async function assertClaimCeiling(page, path, problems) {
  if (path !== "/research/rankings" && path !== "/research/strategy") return;
  const banner = await page.getByRole("note").filter({ hasText: "Not investment advice" }).count();
  if (banner === 0) problems.push("claim-ceiling banner missing");
}

/** (c) OPERATE chrome is exactly the /admin prefix: on all of it, on none of research. */
async function assertOperateChromeMembership(page, path, problems) {
  const chrome = await page.locator("[data-operate-chrome]").count();
  if (path.startsWith("/admin") && chrome === 0) problems.push("OPERATE chrome missing");
  if (path.startsWith("/research") && chrome > 0) problems.push("OPERATE chrome leaked into research");
}

async function checkRoute(page, path) {
  await page.goto(`${BASE}${path}`, { waitUntil: "networkidle" });
  const finalPath = new URL(page.url()).pathname;
  const problems = [];
  if (finalPath === "/login" && path !== "/login") problems.push("bounced to /login while authenticated");

  await assertNoVisibleRawIds(page, path, problems);
  await assertClaimCeiling(page, path, problems);
  await assertOperateChromeMembership(page, path, problems);
  return problems;
}

/** One full pass over every live route under one identity. */
async function walkRoutes(browser, { role, email, password }) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const login = await context.request.post(`${BASE}/api/auth/login`, { data: { email, password } });
  if (login.status() !== 200) {
    console.error(`login failed for ${role}: ${login.status()}`);
    await context.close();
    process.exit(2);
  }
  const page = await context.newPage();

  let failures = 0;
  for (const path of livePages) {
    const problems = await checkRoute(page, path);
    if (problems.length > 0) {
      failures += 1;
      console.log(`FAIL [${role}] ${path} — ${problems.join("; ")}`);
    } else {
      console.log(`ok   [${role}] ${path}`);
    }
  }
  await context.close();
  return failures;
}

const browser = await chromium.launch();
// The administrator pass is the only one this issue arms: it is the identity
// that can reach /admin at all, so it is the one that can assert (c) across the
// whole frozen tree. #371 adds the member pass, which asserts what differs by
// role — nav membership, and the denied state on /admin.
const failures = await walkRoutes(browser, { role: "administrator", email: EMAIL, password: PASSWORD });
await browser.close();

if (failures > 0) {
  console.error(`${failures} route(s) violated the #494 contract`);
  process.exit(1);
}
console.log(`#494 walk-tree passed: ${livePages.length} live routes conform`);
