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
 * Uses playwright (chromium) from the environment; exits non-zero on the
 * first violated assertion, printing every checked route either way.
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

let failures = 0;
const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const login = await context.request.post(`${BASE}/api/auth/login`, {
  data: { email: EMAIL, password: PASSWORD },
});
if (login.status() !== 200) {
  console.error(`login failed: ${login.status()}`);
  process.exit(2);
}
const page = await context.newPage();

for (const path of livePages) {
  await page.goto(`${BASE}${path}`, { waitUntil: "networkidle" });
  const finalPath = new URL(page.url()).pathname;
  const problems = [];
  if (finalPath === "/login" && path !== "/login") problems.push("bounced to /login while authenticated");

  // (a) visible raw ids on research pages — innerText excludes attributes,
  // so title=/href= demotions pass and visible leaks fail.
  if (path.startsWith("/research")) {
    const visible = await page.evaluate(() => {
      const clone = document.body.cloneNode(true);
      for (const el of clone.querySelectorAll("[data-evidence]")) el.remove();
      return clone.innerText || clone.textContent || "";
    });
    const match = visible.match(ID_PATTERN);
    if (match) problems.push(`visible raw id: ${match[0]}`);
  }

  // (b) claim ceiling where strategy output renders
  if (path === "/research/rankings" || path === "/research/strategy") {
    const banner = await page.getByRole("note").filter({ hasText: "Not investment advice" }).count();
    if (banner === 0) problems.push("claim-ceiling banner missing");
  }

  // (c) OPERATE chrome membership
  const chrome = await page.locator("[data-operate-chrome]").count();
  if (path.startsWith("/admin") && chrome === 0) problems.push("OPERATE chrome missing");
  if (path.startsWith("/research") && chrome > 0) problems.push("OPERATE chrome leaked into research");

  if (problems.length > 0) {
    failures += 1;
    console.log(`FAIL ${path} — ${problems.join("; ")}`);
  } else {
    console.log(`ok   ${path}`);
  }
}

await browser.close();
if (failures > 0) {
  console.error(`${failures} route(s) violated the #494 contract`);
  process.exit(1);
}
console.log(`#494 walk-tree passed: ${livePages.length} live routes conform`);
