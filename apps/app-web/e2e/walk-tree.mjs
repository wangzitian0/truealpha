/**
 * #494 acceptance 1: walk every live route in routes.frozen.json against a
 * running deployment and assert the three contract properties:
 *   (a) research first screens show NO raw entity ids as visible text
 *       (ids may appear only in attributes: title/href/data-evidence);
 *   (b) the claim-ceiling banner is present on rankings + strategy (+ the
 *       overview when selections render);
 *   (c) OPERATE chrome on every /admin page, absent on every /research page.
 * #1241 adds a fourth: the trace page of a strategy decision renders its inputs.
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
 * The sign-out journey is the one exception. Since #811 it lives in
 * `sign-out-journey.mjs`, because this file launches a browser when it is
 * imported and `tests/sign-out-journey.test.ts` needs to import the journey.
 */

import { readFileSync } from "node:fs";
import { chromium } from "playwright";
import { runHydrationRace } from "./hydration-race.mjs";
import { createPageErrorLedger, gotoStable } from "./navigation.mjs";
import { walkSignOutJourney } from "./sign-out-journey.mjs";

const BASE = process.env.TA_BASE_URL;
const EMAIL = process.env.TA_EMAIL;
const PASSWORD = process.env.TA_PASSWORD;
// #371: optional second identity. When set, a member pass runs after the
// administrator pass and asserts the properties that DIFFER by role. Absent
// (e.g. against a deployment where only the owner has credentials), the
// administrator pass runs alone exactly as before.
const MEMBER_EMAIL = process.env.TA_MEMBER_EMAIL;
const DATA_STATE = process.env.TA_DATA_STATE || "populated";
if (!BASE || !EMAIL || !PASSWORD) {
  console.error("TA_BASE_URL, TA_EMAIL, TA_PASSWORD are required");
  process.exit(2);
}
// A loopback base URL means the CI walk. It walks the CI build over the database that
// scripts/seed-e2e-fixture.ts seeds (5 golden preview issuers + trace lineage).
const SEEDED_FIXTURE = ["127.0.0.1", "localhost", "[::1]"].includes(new URL(BASE).hostname);
const PREVIEW_TICKERS = ["ADM", "DDOG", "JPM", "NICE", "SHOP"];

const frozen = JSON.parse(readFileSync(new URL("../routes.frozen.json", import.meta.url), "utf8"));
const livePages = frozen.routes
  .filter((r) => r.live && !r.path.includes("/api/") && !r.path.includes("[") && r.path !== "/")
  .map((r) => r.path);

// Every scheme a raw entity id is minted in, not only TOPT's (#877 H5): the planes key
// issuers by CIK and instruments by FIGI, TOPT by LEI and CUSIP. A guard that knew only
// `issuer:lei:` passed a raw QQQ issuer id on a first screen, and would stop catching
// TOPT's too once TOPT resolves to CIK/FIGI. A listing's symbol may carry digits
// (`listing:xnas:t001` in the data-engine fixtures, numeric symbols on other venues).
const ID_PATTERN =
  /(issuer:(?:lei|cik):[A-Z0-9]{10,}|security:(?:cusip|figi):[A-Za-z0-9]{9,}|listing:x[a-z]+:[a-z0-9.]+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i;

/**
 * (a) No raw entity id as VISIBLE text on a research first screen. innerText
 * excludes attributes, so title=/href= demotions pass and visible leaks fail.
 *
 * #1322: `scripts/seed-e2e-fixture.ts` seeds the 5-issuer golden replay (`ADM`,
 * `DDOG`, `JPM`, `NICE`, `SHOP`) with LEI-shaped `legacy-id` aliases and
 * `mart.entity_display_resolution` rows, so this assertion is a real check in
 * PR CI as well as against staging and production.
 */
async function assertNoVisibleRawIds(page, path, problems) {
  if (!path.startsWith("/research")) return;
  // Compute visible text without mutating the live DOM. A DOM node replacement
  // or style mutation before React hydration completes causes Minified React
  // error #418 (HTML mismatch). gotoStable now returns after hydration (#1412),
  // and this read still has no side effect on the page.
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
        tag === "BR" ||
        tag === "P" ||
        tag === "DIV" ||
        tag === "LI" ||
        tag === "TR" ||
        tag === "TD" ||
        tag === "TH"
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

/**
 * (a2) #987 / #1322 Anti-puppet check against GREEN-WHILE-EMPTY: Research data tables
 * must display non-empty tickers/labels rather than blank cells.
 * Under populated data state, populated research routes MUST render at least 1 row,
 * and in the CI seeded fixture `/research/strategy` MUST render all 5 preview tickers.
 */
async function assertNoBlankResearchTableCells(page, path, problems) {
  if (!["/research/rankings", "/research/strategy", "/research/compare", "/research/coverage"].includes(path)) return;
  const result = await page.evaluate(() => {
    const rows = Array.from(document.querySelectorAll("tbody tr"));
    let emptyCount = 0;
    const labels = [];
    for (const row of rows) {
      const entityCell = row.querySelector('th[scope="row"]');
      if (entityCell) {
        const text = entityCell.innerText.trim();
        if (text === "") {
          emptyCount += 1;
        } else {
          labels.push(text);
        }
      }
    }
    return { rowCount: rows.length, emptyCount, labels };
  });

  if (DATA_STATE === "populated" && ["/research/rankings", "/research/strategy"].includes(path)) {
    if (result.rowCount === 0) {
      problems.push(`GREEN-WHILE-EMPTY: expected populated table rows on ${path} in ${DATA_STATE} state, found 0`);
    }
  }

  if (SEEDED_FIXTURE && DATA_STATE === "populated" && path === "/research/strategy") {
    const missingTickers = PREVIEW_TICKERS.filter(
      (ticker) => !result.labels.some((label) => label.startsWith(`${ticker} · `) || label === ticker),
    );
    if (missingTickers.length > 0) {
      problems.push(
        `expected 5 resolved preview tickers on ${path}, missing: ${missingTickers.join(", ")} (rendered: ${result.labels.join(", ")})`,
      );
    }
  }

  if (result.emptyCount > 0) {
    problems.push(`found ${result.emptyCount} table row(s) with blank entity cell (th[scope="row"]) on ${path}`);
  }
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

/** #494 criterion 3: the funnel block says something in EVERY state.
 *
 * `{funnel.kind === "ready" && …}` with no else made the headline L0–L5
 * diagnostic vanish without comment whenever it could not be computed, which
 * reads as "nothing is wrong" rather than "not computed". #495 fixed the code;
 * this is the assertion, and it is deliberately state-agnostic: it does not
 * care whether the funnel computed, only that the region is never empty below
 * its own heading. A CI database cannot compute the funnel, so this exercises
 * the branch that used to render nothing.
 *
 * Anchored on the heading rather than a test id: the two branches render
 * different containers, and asserting through the visible heading means a
 * refactor that drops the heading fails here too, which is the point.
 *
 * Administrator only. A member reaching /admin/quality gets the layout's denied
 * section, which correctly has no funnel — found by walking real staging with
 * both identities, after a local administrator-only run had passed. */
async function assertFunnelSaysSomething(page, path, problems, role) {
  if (path !== "/admin/quality" || role !== "administrator") return;
  // Tolerant of the dash character, surrounding whitespace and capitalisation:
  // the assertion is about the region saying something, and it should not fail
  // because someone typed an en dash or title-cased a word (review).
  const heading = page.getByRole("heading", { name: /L0\s*[-–—]?\s*L5\s+funnel/i });
  if ((await heading.count()) === 0) {
    problems.push("no L0-L5 funnel section on /admin/quality");
    return;
  }
  const section = await heading.first().evaluate((node) => {
    // `closest("div")` rather than parentElement: the ready and non-ready
    // branches render different containers, and wrapping the heading in a
    // <header> later would otherwise shrink the subtree this inspects to the
    // heading itself and make the assertion vacuously pass (review).
    const container = node.closest("div") ?? node.parentElement;
    return { heading: node.innerText.trim(), all: container ? container.innerText.trim() : "" };
  });
  const body = section.all.replace(section.heading, "").trim();
  if (body.length === 0) {
    problems.push("the funnel section renders its heading and nothing else — every state must say something");
  }
}

const SWEEP_RUN_SOURCE_PATTERN = /-[0-9]{8}T[0-9]{4}$/;

/** #1363 / #1362 / #1277: /admin/datahub renders the plant health overview, question coverage
 * matrix, external call ledger traffic, and registered sources without leaking per-run sweep
 * identifiers into the Sources table. */
async function assertDatahubPlantHealth(page, path, problems, role) {
  if (path !== "/admin/datahub" || role !== "administrator") return;
  for (const requiredHeading of [
    /^Datahub$/i,
    /Question coverage matrix/i,
    /Traffic \(external calls, today UTC\)/i,
    /^Sources$/i,
    /^Recent capture runs$/i,
  ]) {
    if ((await page.getByRole("heading", { name: requiredHeading }).count()) === 0) {
      problems.push(`missing /admin/datahub heading matching ${requiredHeading}`);
    }
  }
  const mainText = await page.locator("main").innerText();
  const leak = mainText.match(/\bundefined\b|\[object Object\]|\bNaN\b/);
  if (leak) problems.push(`/admin/datahub shows "${leak[0]}"`);

  const sourcesHeading = page.getByRole("heading", { name: /^Sources$/i });
  if ((await sourcesHeading.count()) > 0) {
    const sourceNames = await sourcesHeading.first().evaluate((node) => {
      const container = node.closest("div") ?? node.parentElement;
      if (!container) return [];
      return Array.from(container.querySelectorAll("tbody tr td:first-child")).map((td) => td.innerText.trim());
    });
    const sweepLeaks = sourceNames.filter((name) => SWEEP_RUN_SOURCE_PATTERN.test(name));
    if (sweepLeaks.length > 0) {
      problems.push(`/admin/datahub Sources table includes timestamped sweep run(s): ${sweepLeaks.join(", ")}`);
    }
  }
}

// #1241. The text that NOT_RECORDED prints in src/server/mart/research-read.ts for a field that the
// lineage does not carry. tests/walk-tree-trace-inputs.test.ts fails when the two texts differ.
const NOT_RECORDED = "not recorded";
// The five cells of one input row. TraceInputsTable marks each cell with data-field.
const TRACE_FIELDS = ["value", "period", "knowable_at", "accession", "source"];

/** #1241 acceptance 1: the trace page of a strategy decision renders its inputs.
 *
 * `/research/trace` without a query shows the empty state. So no walk reached the "Decision inputs"
 * table, and a property that the page never reaches is not asserted. This check opens the first
 * trace link of `/research`, as a person would, and checks the page that follows. It runs in the
 * populated state only. The empty state has no decision to trace.
 *
 * Every environment: the page answers, names itself and prints no script leak. It reaches the ready
 * state, because the link comes from the overview of the same run. Each input row has the five cells,
 * and no cell is blank. When the page names a core result, one input has a value. That input also
 * carries its knowable_at and its accession or source. When the page says "not recorded", no input
 * carries a value.
 *
 * The CI walk only (SEEDED_FIXTURE): the seed gives this decision a lineage. So "Core result: not
 * recorded" is a failure there, and one input must carry all five fields. Without this rule, a read
 * that stopped returning the lineage would pass on the "not recorded" branch.
 *
 * Locators wait for their element. The code uses no fixed sleep. */
async function assertTraceRendersDecisionInputs(page, path, problems) {
  if (path !== "/research/trace" || DATA_STATE !== "populated") return;

  await gotoStable(page, `${BASE}/research`);
  const href = await page
    .locator('a[href^="/research/trace?"]')
    .first()
    .getAttribute("href", { timeout: 15000 })
    .catch(() => null);
  if (href === null) {
    problems.push("trace: /research offers no trace link in the populated state");
    return;
  }

  const response = await gotoStable(page, `${BASE}${href}`);
  const status = response ? response.status() : null;
  if (status === null || status >= 400) problems.push(`trace: ${href} answered HTTP ${status}`);
  const section = page.locator('section[aria-labelledby="trace-heading"]');
  try {
    await section.getByRole("heading", { name: "Output trace", exact: true }).waitFor({ state: "visible", timeout: 15000 });
  } catch {
    problems.push('trace: the heading "Output trace" is not visible');
    return;
  }
  const leak = (await section.innerText()).match(/\bundefined\b|\[object Object\]|\bNaN\b/);
  if (leak) problems.push(`trace: the page shows "${leak[0]}"`);

  // The overview and this page read the same run, so the page must be ready.
  try {
    await section.getByText("Decision inputs", { exact: true }).first().waitFor({ state: "visible", timeout: 15000 });
  } catch {
    const shown = (await section.innerText()).replace(/\s+/g, " ").trim().slice(0, 200);
    problems.push(`trace: no "Decision inputs" section, so the page is not ready: ${shown}`);
    return;
  }
  const coreText = await section
    .getByText(/^Core result:/)
    .first()
    .innerText({ timeout: 15000 })
    .then((text) => text.trim(), () => null);
  if (coreText === null) {
    problems.push('trace: no "Core result:" line under "Decision inputs"');
    return;
  }
  // A recorded id has the shape that the topt_core_results CHECK constraint enforces.
  const coreResult = coreText.match(/^Core result: (topt-core-result:[0-9a-f]{64})\./)?.[1] ?? null;
  if (coreResult === null && !coreText.startsWith(`Core result: ${NOT_RECORDED}.`)) {
    problems.push(`trace: "Core result:" shows neither a result id nor "${NOT_RECORDED}": ${coreText.slice(0, 80)}`);
    return;
  }

  const table = section.locator('table[data-testid="trace-inputs"]');
  try {
    await table.first().waitFor({ state: "attached", timeout: 15000 });
  } catch {
    problems.push("trace: no inputs table (data-testid=trace-inputs)");
    return;
  }
  const rows = await table.locator("tr[data-input]").evaluateAll((trs) =>
    trs.map((tr) => ({
      input: tr.getAttribute("data-input"),
      cells: Object.fromEntries(
        Array.from(tr.querySelectorAll("td[data-field]")).map((td) => [td.getAttribute("data-field"), td.innerText.trim()]),
      ),
    })),
  );
  if (rows.length === 0) {
    problems.push("trace: the inputs table has no input row");
    return;
  }
  const incomplete = rows.filter((row) => TRACE_FIELDS.some((field) => !row.cells[field]));
  if (incomplete.length > 0) {
    problems.push(`trace: input row(s) with a missing or blank cell: ${incomplete.map((row) => row.input).join(", ")}`);
    return;
  }

  const isRecorded = (row, field) => Boolean(row.cells[field]) && row.cells[field] !== NOT_RECORDED;
  const valued = rows.filter((row) => isRecorded(row, "value"));
  // The walk prints the branch that it took. A log with only "ok" cannot tell a filled table from "not recorded".
  console.log(
    `  trace: core result ${coreResult === null ? NOT_RECORDED : "recorded"}, ${rows.length} input rows, ${valued.length} with a value`,
  );
  if (coreResult === null) {
    if (SEEDED_FIXTURE) {
      problems.push(`trace: the seed gives this decision a lineage, but the page says "Core result: ${NOT_RECORDED}"`);
    }
    if (valued.length > 0) {
      problems.push(`trace: the page records no core result, yet these inputs carry a value: ${valued.map((row) => row.input).join(", ")}`);
    }
    return;
  }
  if (valued.length === 0) {
    problems.push(`trace: the page names core result ${coreResult}, but no input carries a value`);
    return;
  }
  if (!valued.some((row) => isRecorded(row, "knowable_at") && (isRecorded(row, "accession") || isRecorded(row, "source")))) {
    problems.push("trace: no input with a value also carries its knowable_at and its accession or source");
  }
  if (SEEDED_FIXTURE && !rows.some((row) => TRACE_FIELDS.every((field) => isRecorded(row, field)))) {
    problems.push(`trace: no input carries all of ${TRACE_FIELDS.join(", ")}; the seed gives gross_profit all five`);
  }
}

/** #1358: Follow the first entity detail link from `/research/rankings` in the populated state so
 * `/research/entities/[id]` (which is excluded from `livePages` because its route pattern contains `[`)
 * is exercised in every populated e2e walk. */
async function assertEntityDetailRenders(page, path, problems) {
  if (path !== "/research/rankings" || DATA_STATE !== "populated") return;

  const href = await page
    .locator('a[href^="/research/entities/"]')
    .first()
    .getAttribute("href", { timeout: 15000 })
    .catch(() => null);
  if (href === null) {
    problems.push("entity-detail: /research/rankings offers no /research/entities/[id] link in the populated state");
    return;
  }

  const response = await gotoStable(page, `${BASE}${href}`);
  const status = response ? response.status() : null;
  if (status === null || status >= 400) {
    problems.push(`entity-detail: ${href} answered HTTP ${status}`);
    return;
  }
  const section = page.locator('section[aria-labelledby="entity-heading"]');
  try {
    await section.locator("#entity-heading").waitFor({ state: "visible", timeout: 15000 });
  } catch {
    problems.push(`entity-detail: #entity-heading is not visible on ${href}`);
    return;
  }
  await assertNoVisibleRawIds(page, href, problems);
  const leak = (await section.innerText()).match(/\bundefined\b|\[object Object\]|\bNaN\b/);
  if (leak) problems.push(`entity-detail: ${href} shows "${leak[0]}"`);
}

async function checkRoute(page, path, role, pageErrors) {
  pageErrors.startRoute(role, path);
  await gotoStable(page, `${BASE}${path}`);
  const finalPath = new URL(page.url()).pathname;
  const problems = [];
  if (finalPath === "/login" && path !== "/login") problems.push("bounced to /login while authenticated");

  await assertNoVisibleRawIds(page, path, problems);
  await assertNoBlankResearchTableCells(page, path, problems);
  await assertClaimCeiling(page, path, problems);
  await assertOperateChromeMembership(page, path, problems, role);
  await assertWorldSwitch(page, path, problems, role);
  await assertSignedInIdentity(page, path, problems);
  await assertCurrentNavLink(page, path, problems);
  await assertFunnelSaysSomething(page, path, problems, role);
  await assertDatahubPlantHealth(page, path, problems, role);
  // Last, because these navigate away from this page.
  await assertEntityDetailRenders(page, path, problems);
  await assertTraceRendersDecisionInputs(page, path, problems);
  problems.push(...pageErrors.problems());
  return problems;
}

/** One full pass over every live route under one identity. */
async function walkRoutes(browser, { role, email, password }, pageErrors) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const login = await context.request.post(`${BASE}/api/auth/login`, { data: { email, password } });
  if (login.status() !== 200) {
    console.error(`login failed for ${role}: ${login.status()}`);
    await context.close();
    process.exit(2);
  }
  // Warm heavy research routes under the authenticated session to avoid cold render timeouts.
  for (const warmPath of ["/research", "/research/coverage", "/research/rankings", "/research/strategy"]) {
    try {
      await context.request.get(`${BASE}${warmPath}`, { timeout: 15000 });
    } catch {
      // Best-effort warm-up; checkRoute performs authoritative assertions.
    }
  }
  const page = await context.newPage();
  // Every page error fails the route, React #418 included (#1468).
  page.on("pageerror", (error) => {
    pageErrors.record(error.message || String(error), new URL(page.url()).pathname);
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
  await context.close();
  return failures;
}

const browser = await chromium.launch();
const pageErrors = createPageErrorLedger();
let failures = 0;
try {
  // Two passes. The administrator pass asserts (c) across the whole frozen tree —
  // it is the only identity that can see operator chrome at all. The member pass
  // (#371) asserts the mirror image: no chrome, no world switch, and the same
  // identity/current-page properties everywhere else.
  failures += await walkRoutes(browser, { role: "administrator", email: EMAIL, password: PASSWORD }, pageErrors);
  if (MEMBER_EMAIL) {
    failures += await walkRoutes(browser, { role: "member", email: MEMBER_EMAIL, password: PASSWORD }, pageErrors);
  }
  // #540/#811: the sign-out journey lives in its own module so a test can drive it
  // against a fake browser. It logs in once more, for its own session.
  failures += await walkSignOutJourney(browser, { base: BASE, email: EMAIL, password: PASSWORD, gotoStable });
  // #1468: the CI server delivers every flight row in one burst, so only this pass opens the
  // window in which the deployed surface raised React #418. It runs against the seeded fixture
  // only: the deployed surface has the window by itself, and the proxy cannot sit in front of it.
  if (SEEDED_FIXTURE && DATA_STATE === "populated") {
    const race = await runHydrationRace({ browser, base: BASE, email: EMAIL, password: PASSWORD });
    for (const problem of race.problems) console.log(`FAIL ${problem}`);
    if (race.problems.length > 0) {
      failures += 1;
    } else {
      console.log(`ok   [hydration-race] ${race.visits} visits, each with a late flight row, raised no page error`);
    }
  }
} finally {
  await browser.close();
}

if (failures > 0) {
  console.error(`${failures} route(s) violated the #494 contract`);
  process.exit(1);
}
console.log(`#494 walk-tree passed: ${livePages.length} live routes conform`);
