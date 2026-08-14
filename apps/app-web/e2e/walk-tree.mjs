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
// #371: optional second identity. When set, a member pass runs after the
// administrator pass and asserts the properties that DIFFER by role. Absent
// (e.g. against a deployment where only the owner has credentials), the
// administrator pass runs alone exactly as before.
const MEMBER_EMAIL = process.env.TA_MEMBER_EMAIL;
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

/** (c) OPERATE chrome is exactly the /admin prefix — for an identity that may
 * see it. A member reaching /admin gets the explicit denied state instead, and
 * must NOT be shown operator chrome. */
async function assertOperateChromeMembership(page, path, problems, role) {
  const chrome = await page.locator("[data-operate-chrome]").count();
  if (path.startsWith("/admin")) {
    if (role === "administrator" && chrome === 0) problems.push("OPERATE chrome missing");
    if (role !== "administrator" && chrome > 0) problems.push("OPERATE chrome shown to a non-administrator");
  }
  if (path.startsWith("/research") && chrome > 0) problems.push("OPERATE chrome leaked into research");
}

/** #371: the world switch, both legs, role-dependent. This is the criterion
 * that closed as met and was never true: there was no link to /admin anywhere
 * and no link back. */
async function assertWorldSwitch(page, path, problems, role) {
  if (path.startsWith("/research")) {
    const toAdmin = await page.locator('[data-world-switch="admin"]').count();
    if (role === "administrator" && toAdmin === 0) problems.push("no world switch to /admin");
    if (role !== "administrator" && toAdmin > 0) problems.push("world switch to /admin offered to a non-administrator");
  }
  if (path.startsWith("/admin") && role === "administrator") {
    const back = await page.locator('[data-world-switch="research"]').count();
    if (back === 0) problems.push("no way back to /research from the operate world");
  }
}

/** #371: an authenticated page says who is signed in.
 *
 * `/login` is skipped rather than asserted-empty: this suite walks it while
 * holding a session, and the top bar correctly shows that session there. What
 * an ANONYMOUS visitor sees on /login is not something a logged-in walk can
 * observe — AppChrome returns null without a principal, which is a server-side
 * property the auth tests already cover. */
async function assertSignedInIdentity(page, path, problems) {
  if (path === "/login") return;
  const shown = await page.locator("[data-signed-in-as]").allInnerTexts();
  const identity = shown.map((t) => t.trim()).filter((t) => t.length > 0);
  if (identity.length === 0) problems.push("no signed-in identity in the top bar");
}

/** #371: the nav says where you are. Seven identical pills told you nothing,
 * by sight or by screen reader.
 *
 * The nav declares its own destinations, so this does not duplicate the link
 * list and cannot drift from it. On a page that IS a destination exactly one
 * link is current and it is this one; on a page that is not — /research/trace
 * is the evidence viewer, reached from trace chips and deliberately absent
 * from the nav (#493's tree) — no link may claim to be current. */
async function assertCurrentNavLink(page, path, problems) {
  if (!path.startsWith("/research")) return;
  const nav = page.locator('nav[aria-label="Research sections"]');
  if ((await nav.count()) === 0) {
    problems.push("no research nav rendered");
    return;
  }
  const destinations = await nav.locator("a").evaluateAll((links) =>
    links.map((a) => new URL(a.href).pathname),
  );
  const current = nav.locator('[aria-current="page"]');
  const count = await current.count();

  if (!destinations.includes(path)) {
    if (count !== 0) problems.push(`${path} is not a nav destination but ${count} link(s) claim to be current`);
    return;
  }
  if (count !== 1) {
    problems.push(`expected exactly 1 nav link marked aria-current="page", found ${count}`);
    return;
  }
  const href = await current.first().getAttribute("href");
  if (href !== path) problems.push(`nav marks ${href} current on ${path}`);
}

async function checkRoute(page, path, role) {
  await page.goto(`${BASE}${path}`, { waitUntil: "networkidle" });
  const finalPath = new URL(page.url()).pathname;
  const problems = [];
  if (finalPath === "/login" && path !== "/login") problems.push("bounced to /login while authenticated");

  await assertNoVisibleRawIds(page, path, problems);
  await assertClaimCeiling(page, path, problems);
  await assertOperateChromeMembership(page, path, problems, role);
  await assertWorldSwitch(page, path, problems, role);
  await assertSignedInIdentity(page, path, problems);
  await assertCurrentNavLink(page, path, problems);
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
    const problems = await checkRoute(page, path, role);
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
// Two passes. The administrator pass asserts (c) across the whole frozen tree —
// it is the only identity that can see operator chrome at all. The member pass
// (#371) asserts the mirror image: no chrome, no world switch, and the same
// identity/current-page properties everywhere else.
let failures = await walkRoutes(browser, { role: "administrator", email: EMAIL, password: PASSWORD });
if (MEMBER_EMAIL) {
  failures += await walkRoutes(browser, { role: "member", email: MEMBER_EMAIL, password: PASSWORD });
}
await browser.close();

if (failures > 0) {
  console.error(`${failures} route(s) violated the #494 contract`);
  process.exit(1);
}
console.log(`#494 walk-tree passed: ${livePages.length} live routes conform`);
