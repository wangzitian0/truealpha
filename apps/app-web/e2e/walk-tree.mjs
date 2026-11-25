/**
 * #494 / Wave 4 thin live route smoke: discover every static live page under
 * `src/app` automatically and verify HTTP status, hydration, zero JS/console
 * errors, zero error states, zero undefined/NaN/[object Object] leaks, zero
 * visible raw entity IDs, claim-ceiling on rankings, and role boundaries.
 *
 * Runs against ANY environment:
 *   APP_URL=https://truealpha-staging.truealpha.club \
 *   TA_EMAIL=owner@truealpha.club TA_PASSWORD=… \
 *   node e2e/walk-tree.mjs
 */

import { setDefaultResultOrder } from "node:dns";
import { readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { DETAIL_LINK_TIMEOUT_MS, findDetailLink } from "./detail-link.mjs";
import { createPageErrorLedger, gotoStable } from "./navigation.mjs";
import { preflightIdentities } from "./preflight.mjs";
import { assertNoReadError } from "./read-error.mjs";
import { walkSignOutJourney } from "./sign-out-journey.mjs";

const BASE = process.env.APP_URL || process.env.TA_BASE_URL;
const EMAIL = process.env.TA_EMAIL;
const PASSWORD = process.env.TA_PASSWORD;
const MEMBER_EMAIL = process.env.TA_MEMBER_EMAIL;
const DATA_STATE = process.env.TA_DATA_STATE || "populated";
if (!BASE || !EMAIL || !PASSWORD) {
  console.error("APP_URL (or TA_BASE_URL), TA_EMAIL, TA_PASSWORD are required");
  process.exit(2);
}

const APP_DIR = fileURLToPath(new URL("../src/app", import.meta.url));

/** Discover every static live page route under `src/app` without a manual route freeze. */
function discoverStaticPages(dir, segments = []) {
  const routes = [];
  for (const entry of readdirSync(dir)) {
    if (entry === "api" || entry.startsWith("_")) continue;
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (entry.includes("[")) continue;
      const nextSegments = entry.startsWith("(") && entry.endsWith(")") ? segments : [...segments, entry];
      routes.push(...discoverStaticPages(full, nextSegments));
    } else if (entry === "page.tsx" && segments.length > 0) {
      routes.push(`/${segments.join("/")}`);
    }
  }
  return routes;
}

const livePages = discoverStaticPages(APP_DIR).sort();
if (livePages.length === 0) {
  console.error(`no live routes discovered under ${APP_DIR}`);
  process.exit(2);
}

const ID_PATTERN =
  /(issuer:(?:lei|cik):[A-Z0-9]{10,}|security:(?:cusip|figi):[A-Za-z0-9]{9,}|listing:x[a-z]+:[a-z0-9.]+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i;
const VALUE_LEAK_PATTERN = /\bundefined\b|\[object Object\]|\bNaN\b/;

/** Reject visible leaked JS values (`undefined`, `NaN`, `[object Object]`) on any rendered surface. */
async function assertNoValueLeaks(page, path, problems) {
  const main = page.locator("main");
  const text = (await main.count()) > 0 ? await main.first().innerText() : await page.locator("body").innerText();
  const leak = text.match(VALUE_LEAK_PATTERN);
  if (leak) problems.push(`${path} shows "${leak[0]}"`);
}

/** Reject raw entity IDs rendered as visible text on /research pages without mutating live DOM. */
async function assertNoVisibleRawIds(page, path, problems) {
  if (!path.startsWith("/research")) return;
  const visible = await page.evaluate(() => {
    const pieces = [];
    function collect(node) {
      if (node.nodeType === Node.TEXT_NODE) {
        pieces.push(node.nodeValue || "");
        return;
      }
      if (node.nodeType !== Node.ELEMENT_NODE) return;
      const el = node;
      const tag = el.tagName;
      if (tag === "SCRIPT" || tag === "STYLE" || tag === "NOSCRIPT" || tag === "TEMPLATE") return;
      if (el.hasAttribute("data-evidence") || el.hasAttribute("hidden")) return;
      const style = window.getComputedStyle(el);
      if (style.display === "none" || style.visibility === "hidden") return;
      for (const child of el.childNodes) {
        collect(child);
      }
      if (
        style.display === "block" ||
        style.display === "flex" ||
        style.display === "grid" ||
        style.display === "table-row" ||
        style.display === "table-cell" ||
        ["BR", "P", "DIV", "LI", "TR", "TD", "TH"].includes(tag)
      ) {
        pieces.push(" ");
      }
    }
    if (document.body) collect(document.body);
    return pieces.join("");
  });
  const match = visible.match(ID_PATTERN);
  if (match) problems.push(`visible raw id: ${match[0]}`);
}

/** Log research table row counts as non-blocking operational facts. */
async function logResearchRowCount(page, path, role) {
  if (!["/research/rankings", "/research/coverage"].includes(path)) return;
  const rowCount = await page.locator("tbody tr").count();
  console.log(`  fact [${role}] ${path} (${DATA_STATE}): ${rowCount} table row(s)`);
}

/** Claim-ceiling banner remains present on strategy rankings output. */
async function assertClaimCeiling(page, path, problems) {
  if (path !== "/research/rankings") return;
  const banner = await page.getByRole("note").filter({ hasText: "Not investment advice" }).count();
  if (banner === 0) problems.push("claim-ceiling banner missing");
}

/** OPERATE chrome stays strictly inside /admin for administrators only. */
async function assertOperateChromeMembership(page, path, problems, role) {
  const chrome = await page.locator("[data-operate-chrome]").count();
  if (path.startsWith("/admin")) {
    if (role === "administrator" && chrome === 0) problems.push("OPERATE chrome missing");
    if (role !== "administrator" && chrome > 0) problems.push("OPERATE chrome shown to a non-administrator");
  }
  if (path.startsWith("/research") && chrome > 0) problems.push("OPERATE chrome leaked into research");
}

/** World switch links stay gated by role across /research and /admin. */
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

/** Authenticated routes always show the active principal identity in app chrome. */
async function assertSignedInIdentity(page, path, problems) {
  if (path === "/login") return;
  const shown = await page.locator("[data-signed-in-as]").allInnerTexts();
  const identity = shown.map((t) => t.trim()).filter((t) => t.length > 0);
  if (identity.length === 0) problems.push("no signed-in identity in the top bar");
}

/** Research section nav renders and marks only the matching active destination. */
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

/** Follow the first dynamic `[id]` link when present so dynamic routes are smoke-tested when data exists. */
async function assertDynamicDetailRenders(page, path, problems, spec) {
  if (path !== spec.listPath || DATA_STATE !== "populated") return;

  const found = await findDetailLink(page, spec.hrefPrefix);
  if (found.kind === "none") {
    problems.push(
      `${spec.name}: ${spec.listPath} shows no ${spec.routePattern} link, no empty-list notice and no read-error notice within ${DETAIL_LINK_TIMEOUT_MS / 1000} s`,
    );
    return;
  }
  if (found.kind === "read-error") {
    problems.push(`${spec.name}: ${spec.listPath} shows a read-error notice in place of its list`);
    return;
  }
  if (found.kind === "empty") {
    console.log(`  fact [${spec.name}]: ${spec.listPath} rendered empty notice, skipping dynamic detail walk`);
    return;
  }

  const href = found.href;
  const response = await gotoStable(page, `${BASE}${href}`);
  const status = response ? response.status() : null;
  if (status === null || status >= 400) {
    problems.push(`${spec.name}: ${href} answered HTTP ${status}`);
    return;
  }
  await assertNoReadError(page, href, problems);
  const section = page.locator(`section[aria-labelledby="${spec.headingId}"]`);
  try {
    await section.locator(`#${spec.headingId}`).waitFor({ state: "visible", timeout: 15000 });
  } catch {
    problems.push(`${spec.name}: #${spec.headingId} is not visible on ${href}`);
    return;
  }
  await assertNoVisibleRawIds(page, href, problems);
  const leak = (await section.innerText()).match(VALUE_LEAK_PATTERN);
  if (leak) problems.push(`${spec.name}: ${href} shows "${leak[0]}"`);
}

const DYNAMIC_DETAIL_SPECS = [
  {
    name: "entity-detail",
    listPath: "/research/rankings",
    hrefPrefix: "/research/entities/",
    routePattern: "/research/entities/[id]",
    headingId: "entity-heading",
  },
  {
    name: "backtest-detail",
    listPath: "/research/backtest",
    hrefPrefix: "/research/backtest/",
    routePattern: "/research/backtest/[id]",
    headingId: "backtest-detail-heading",
  },
  {
    name: "library-detail",
    listPath: "/research/library",
    hrefPrefix: "/research/library/",
    routePattern: "/research/library/[id]",
    headingId: "doc-detail-heading",
  },
];

async function checkRoute(page, path, role, pageErrors) {
  pageErrors.startRoute(role, path);
  const response = await gotoStable(page, `${BASE}${path}`);
  const status = response ? response.status() : null;
  const finalPath = new URL(page.url()).pathname;
  const problems = [];
  if (status === null || status >= 400) problems.push(`HTTP ${status}`);
  if (finalPath === "/login" && path !== "/login") problems.push("bounced to /login while authenticated");
  await assertNoReadError(page, path, problems);
  await assertNoValueLeaks(page, path, problems);
  await assertNoVisibleRawIds(page, path, problems);
  await logResearchRowCount(page, path, role);
  await assertClaimCeiling(page, path, problems);
  await assertOperateChromeMembership(page, path, problems, role);
  await assertWorldSwitch(page, path, problems, role);
  await assertSignedInIdentity(page, path, problems);
  await assertCurrentNavLink(page, finalPath, problems);
  // Dynamic detail links navigate away from the current list page, so inspect them last.
  for (const spec of DYNAMIC_DETAIL_SPECS) {
    await assertDynamicDetailRenders(page, path, problems, spec);
  }
  problems.push(...pageErrors.problems());
  return problems;
}

/** Walk every discovered live static route under one already-authenticated identity. */
async function walkRoutes(role, context, pageErrors) {
  for (const warmPath of ["/research/coverage", "/research/rankings", "/research/themes"]) {
    try {
      await context.request.get(`${BASE}${warmPath}`, { timeout: 15000 });
    } catch {
      // Best-effort warm-up; checkRoute performs authoritative assertions.
    }
  }
  const page = await context.newPage();
  page.on("pageerror", (error) => {
    pageErrors.record(error.message || String(error), new URL(page.url()).pathname);
  });
  page.on("console", (msg) => {
    if (msg.type() === "error") {
      pageErrors.record(`console.error: ${msg.text()}`, new URL(page.url()).pathname);
    }
  });

  let failures = 0;
  for (const path of livePages) {
    const problems = await checkRoute(page, path, role, pageErrors);
    if (problems.length > 0) {
      failures += 1;
      console.log(`FAIL [${role}] ${path} — ${problems.join("; ")}`);
    } else {
      console.log(`ok   [${role}] ${path}`);
    }
  }
  for (const problem of pageErrors.lateProblems()) {
    failures += 1;
    console.log(`FAIL [${role}] ${problem}`);
  }
  await context.close();
  return failures;
}

setDefaultResultOrder("ipv4first");
const browser = await chromium.launch();
const pageErrors = createPageErrorLedger();
let failures = 0;
let sessions = [];
try {
  const identities = [{ role: "administrator", email: EMAIL, password: PASSWORD }];
  if (MEMBER_EMAIL) {
    identities.push({ role: "member", email: MEMBER_EMAIL, password: PASSWORD });
  }
  sessions = await preflightIdentities(browser, identities, { base: BASE });
  for (const session of sessions) {
    failures += await walkRoutes(session.role, session.context, pageErrors);
  }
  failures += await walkSignOutJourney(browser, { base: BASE, email: EMAIL, password: PASSWORD, gotoStable });
} finally {
  await browser.close();
}

if (failures > 0) {
  console.error(`${failures} route(s) failed live smoke checks`);
  process.exit(1);
}
console.log(`live route smoke passed: ${livePages.length} discovered static routes conform`);
