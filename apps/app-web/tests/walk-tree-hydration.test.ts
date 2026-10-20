/**
 * #1412 W2: every walk navigation waits until the page has hydrated, and the
 * walk shows the React #418 events that it ignores.
 *
 * Nine staging walks (v0.2.18 to v0.2.21) failed on "Minified React error
 * #418" on routes that changed from run to run. `gotoStable` waited for
 * hydration only when a sign-out control was on the page, and a
 * `.catch(() => null)` dropped a timeout of that wait. So a walk could read,
 * leave or click a page before React had hydrated it, and an error of the
 * page's hydration could reach the next route.
 *
 * This drives `gotoStable` of `e2e/navigation.mjs` against a fake page. The
 * fake reads each selector by parsing it against the attributes of its two
 * elements (`html` and `main`). It does not import the walk's selector
 * constant, so a `gotoStable` that waits for another selector, or for none,
 * fails here. `tests/hydration-marker.test.ts` proves the other half: the app
 * sets the attribute that this selector reads.
 *
 * Run standalone: `bun run tests/walk-tree-hydration.test.ts`.
 */

import { readFileSync, readdirSync } from "node:fs";
import * as navigation from "../e2e/navigation.mjs";

function assert(condition: unknown, message: string): asserts condition {
	if (!condition) throw new Error(message);
}

const BASE = "http://walk.invalid";
const POLL_MS = 5;
const NEVER = Number.POSITIVE_INFINITY;
// Small enough to run fast. Hydration in the healthy scenarios happens at
// 20 ms or 200 ms, inside the 1 s bound, so timer jitter cannot turn a pass into a fail.
const TIMEOUTS = { gotoMs: 1000, mainMs: 1000, hydrationMs: 1000 };
const SHORT_HYDRATION = { ...TIMEOUTS, hydrationMs: 60 };

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/** `tag`, `[a]`, `[a="v"]`, `[a='v']`, and a comma list of these. Anything else
 * is a selector the fake does not understand, and the test fails loudly. */
function matches(selector: string, tag: string, attributes: Record<string, string>): boolean {
	return selector.split(",").some((part) => {
		const compound = part.trim().match(/^([a-z]+)?((?:\[[\w-]+(?:=(?:"[^"]*"|'[^']*'))?\])*)$/);
		assert(compound !== null && compound[0] !== "", `the fake page cannot read selector ${selector}`);
		if (compound[1] !== undefined && compound[1] !== tag) return false;
		const conditions = [...compound[2].matchAll(/\[([\w-]+)(?:=(?:"([^"]*)"|'([^']*)'))?\]/g)];
		return conditions.every(([, name, double, single]) => {
			const expected = double ?? single;
			return name in attributes && (expected === undefined || attributes[name] === expected);
		});
	});
}

class FakeResponse {
	constructor(private readonly code: number) {}
	status(): number {
		return this.code;
	}
}

/** One page. Each navigation renders `main` at once and hydrates after the
 * delay that the scenario gives for that attempt. */
class FakePage {
	readonly visits: string[] = [];
	private current = "about:blank";
	private hydratedAt = NEVER;
	constructor(private readonly hydrationDelayMs: number[]) {}

	url(): string {
		return this.current;
	}

	async goto(url: string, options: { waitUntil?: string; timeout: number }): Promise<FakeResponse> {
		assert(options.waitUntil === "domcontentloaded", `gotoStable waits for ${options.waitUntil}, not domcontentloaded`);
		const delay = this.hydrationDelayMs[this.visits.length] ?? NEVER;
		this.visits.push(url);
		this.current = url;
		this.hydratedAt = Date.now() + delay;
		return new FakeResponse(200);
	}

	/** The elements in the DOM now, as [tag, attributes]. */
	private elements(): Array<[string, Record<string, string>]> {
		if (this.current === "about:blank") return [];
		const html: Record<string, string> = { lang: "en" };
		if (Date.now() >= this.hydratedAt) html["data-hydrated"] = "true";
		return [
			["html", html],
			["main", {}],
		];
	}

	private count(selector: string): number {
		return this.elements().filter(([tag, attributes]) => matches(selector, tag, attributes)).length;
	}

	locator(selector: string) {
		return { count: async () => this.count(selector) };
	}

	async waitForSelector(selector: string, options: { state?: string; timeout: number }): Promise<void> {
		assert(options.state === "attached", `gotoStable waits for ${selector} to be ${options.state}, not attached`);
		const deadline = Date.now() + options.timeout;
		while (this.count(selector) === 0) {
			if (Date.now() >= deadline) {
				// Playwright's message has the same shape: a first line, then a call log.
				throw new Error(
					`page.waitForSelector: Timeout ${options.timeout}ms exceeded.\nCall log:\n  - waiting for locator('${selector}')`,
				);
			}
			await sleep(POLL_MS);
		}
	}
}

async function navigate(path: string, hydrationDelayMs: number[], timeouts = TIMEOUTS) {
	const page = new FakePage(hydrationDelayMs);
	const lines: string[] = [];
	const started = Date.now();
	const outcome = await navigation
		.gotoStable(page, `${BASE}${path}`, { timeouts, log: (line: string) => lines.push(line) })
		.then(
			(response: unknown) => ({ kind: "resolved" as const, response }),
			(error: unknown) => ({ kind: "rejected" as const, message: String((error as Error)?.message ?? error) }),
		);
	return { outcome, page, lines, elapsedMs: Date.now() - started };
}

// ─── (b1) the marker never appears: gotoStable rejects and names the route ───
{
	const { outcome, page, lines, elapsedMs } = await navigate("/research/themes", [NEVER, NEVER], SHORT_HYDRATION);
	assert(
		outcome.kind === "rejected",
		'gotoStable resolved for /research/themes although html[data-hydrated="true"] never appeared, so the walk acts on a page that React has not hydrated (#1412 W2)',
	);
	assert(outcome.message.includes("/research/themes"), `the hydration failure does not name the route: ${outcome.message}`);
	assert(
		outcome.message.includes('html[data-hydrated="true"]'),
		`the hydration failure does not name the marker that it waited for: ${outcome.message}`,
	);
	assert(!outcome.message.includes("Call log"), `the failure carries Playwright's call log, not one line per attempt: ${outcome.message}`);
	assert(page.visits.length === 2, `a hydration timeout gets one retry; gotoStable navigated ${page.visits.length} times`);
	// Each attempt waits at most hydrationMs (60 ms), so 2 attempts end far below 1 s.
	assert(elapsedMs < 900, `the hydration wait is not bounded by the timeout that it was given: ${elapsedMs} ms`);
	assert(
		lines.length === 1 && lines[0].includes("/research/themes") && lines[0].includes("attempt 1"),
		`the first failed attempt must be printed with its route, so a retry is never silent: ${JSON.stringify(lines)}`,
	);
}

// ─── (b2) the route in the message keeps its query ───────────────────────────
{
	const { outcome } = await navigate("/research/trace?run=r-1&decision=d-2", [NEVER, NEVER], SHORT_HYDRATION);
	assert(outcome.kind === "rejected", "gotoStable resolved for a trace page that never hydrated");
	assert(
		outcome.message.includes("/research/trace?run=r-1&decision=d-2"),
		`the failure must name the trace link that the walk followed: ${outcome.message}`,
	);
}

// ─── (b3) a healthy page: hydration arrives after main, gotoStable waits for it
{
	// 200 ms: a gotoStable that does not wait returns long before this.
	const page = new FakePage([200]);
	const response = await navigation.gotoStable(page, `${BASE}/research/rankings`, { timeouts: TIMEOUTS, log: () => {} });
	assert(response instanceof FakeResponse && response.status() === 200, "gotoStable must return the response of its navigation (#1241)");
	assert(
		(await page.locator('html[data-hydrated="true"]').count()) === 1,
		"gotoStable returned before the page hydrated, so the next step can leave or read a page that React has not hydrated",
	);
	assert(page.visits.length === 1, `a healthy page needs one navigation; gotoStable made ${page.visits.length}`);
}

// ─── (b4) slow first attempt, hydrated second attempt: one printed retry ─────
{
	const { outcome, page, lines } = await navigate("/research/strategy", [NEVER, 20], SHORT_HYDRATION);
	assert(outcome.kind === "resolved", `the retry hydrated, so gotoStable must resolve: ${JSON.stringify(outcome)}`);
	assert(page.visits.length === 2, `expected one retry; gotoStable navigated ${page.visits.length} times`);
	assert(lines.length === 1 && lines[0].includes("/research/strategy"), `the retry must be printed: ${JSON.stringify(lines)}`);
}

// ─── (c) the #418 filter stays visible: a count per route ────────────────────
{
	const REACT_418 = "Minified React error #418; visit https://react.dev/errors/418?args[]=text for the full message";
	const ledger = navigation.createPageErrorLedger();
	assert(
		JSON.stringify(ledger.ignoredReport()) === JSON.stringify(["React #418 events that the walk ignored: 0"]),
		`a walk without #418 must still print its count: ${JSON.stringify(ledger.ignoredReport())}`,
	);

	ledger.startRoute("administrator", "/research/trace");
	ledger.record(REACT_418, "/research/trace");
	// assertTraceRendersDecisionInputs opens /research first, so the event names that page too.
	ledger.record(REACT_418, "/research");
	ledger.record("TypeError: Cannot read properties of undefined (reading 'value')", "/research/trace");
	// The filter knows #418 only: #4180 and #423 are failures.
	ledger.record("Minified React error #4180", "/research/trace");
	ledger.record("Minified React error #423", "/research/trace");
	assert(
		JSON.stringify(ledger.failures) ===
			JSON.stringify([
				"TypeError: Cannot read properties of undefined (reading 'value')",
				"Minified React error #4180",
				"Minified React error #423",
			]),
		`only #418 is ignored, and every other page error fails the route: ${JSON.stringify(ledger.failures)}`,
	);

	ledger.startRoute("member", "/research/rankings");
	assert(ledger.failures.length === 0, `a new route starts with no page errors: ${JSON.stringify(ledger.failures)}`);
	ledger.record(REACT_418, "/research/rankings");

	const report = ledger.ignoredReport();
	assert(
		JSON.stringify(report) ===
			JSON.stringify([
				"React #418 events that the walk ignored: 3",
				"  [administrator] /research/trace: 1",
				"  [administrator] /research/trace on /research: 1",
				"  [member] /research/rankings: 1",
			]),
		`the walk must print each ignored #418 under its route: ${JSON.stringify(report)}`,
	);
}

// ─── (d) every walk navigation goes through gotoStable ───────────────────────
{
	const e2e = new URL("../e2e/", import.meta.url);
	const scripts = readdirSync(e2e).filter((name) => name.endsWith(".mjs"));
	assert(scripts.includes("walk-tree.mjs") && scripts.includes("navigation.mjs"), `unexpected e2e scripts: ${scripts.join(", ")}`);
	for (const name of scripts) {
		if (name === "navigation.mjs") continue;
		const source = readFileSync(new URL(name, e2e), "utf8");
		assert(!/\.goto\(/.test(source), `e2e/${name} calls goto() itself; navigate with gotoStable so the page hydrates first`);
		assert(!/function gotoStable\b/.test(source), `e2e/${name} declares its own gotoStable; import it from navigation.mjs`);
	}
	const walk = readFileSync(new URL("walk-tree.mjs", e2e), "utf8");
	assert(
		/import \{[^}]*\bgotoStable\b[^}]*\} from "\.\/navigation\.mjs";/.test(walk),
		"e2e/walk-tree.mjs does not import gotoStable from navigation.mjs",
	);
}

console.log("#1412 walk-tree hydration wait passed");
