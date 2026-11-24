/**
 * #1412 W2: every walk navigation waits until the page has hydrated.
 * #1468: the walk ignores no page error. React #418 fails its route.
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
// errorPageMs: 1000 also guards (b1): a gotoStable that waited for an error page after a
// failure that is not a network error would take at least 1 s there.
const TIMEOUTS = { gotoMs: 1000, mainMs: 1000, hydrationMs: 1000, errorPageMs: 1000 };
// #1856: the time from a network error to the commit of Chromium's error page.
const ERROR_PAGE_COMMIT_MS = 30;
const CHROME_ERROR_PAGE = "chrome-error://chromewebdata/";
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
 * delay that the scenario gives for that attempt.
 *
 * #1856: a visit listed in `networkErrorVisits` fails like Chromium does on a
 * connection reset. `goto` throws `net::ERR_CONNECTION_RESET`, and the page
 * commits Chromium's error page ERROR_PAGE_COMMIT_MS later. A `goto` that starts
 * before that commit is interrupted by it, as measured in a real browser. */
class FakePage {
	readonly visits: string[] = [];
	private current = "about:blank";
	private hydratedAt = NEVER;
	private errorPageAt = NEVER;
	constructor(
		private readonly hydrationDelayMs: number[],
		private readonly networkErrorVisits: number[] = [],
	) {}

	/** Commit Chromium's error page once its time has come. */
	private settle(): void {
		if (Date.now() >= this.errorPageAt) {
			this.current = CHROME_ERROR_PAGE;
			this.errorPageAt = NEVER;
		}
	}

	url(): string {
		this.settle();
		return this.current;
	}

	async goto(url: string, options: { waitUntil?: string; timeout: number }): Promise<FakeResponse> {
		assert(options.waitUntil === "domcontentloaded", `gotoStable waits for ${options.waitUntil}, not domcontentloaded`);
		this.settle();
		const visit = this.visits.length;
		this.visits.push(url);
		if (this.errorPageAt !== NEVER) {
			throw new Error(`page.goto: Navigation to "${url}" is interrupted by another navigation to "${CHROME_ERROR_PAGE}"`);
		}
		if (this.networkErrorVisits.includes(visit)) {
			this.errorPageAt = Date.now() + ERROR_PAGE_COMMIT_MS;
			throw new Error(`page.goto: net::ERR_CONNECTION_RESET at ${url}\nCall log:\n  - navigating to "${url}", waiting until "domcontentloaded"`);
		}
		const delay = this.hydrationDelayMs[visit] ?? NEVER;
		this.current = url;
		this.hydratedAt = Date.now() + delay;
		return new FakeResponse(200);
	}

	async waitForURL(predicate: (url: URL) => boolean, options: { timeout: number; waitUntil?: string }): Promise<void> {
		assert(options.waitUntil === "commit", `the error-page wait must end at commit, not ${options.waitUntil}`);
		const deadline = Date.now() + options.timeout;
		while (!predicate(new URL(this.url()))) {
			if (Date.now() >= deadline) throw new Error(`page.waitForURL: Timeout ${options.timeout}ms exceeded.`);
			await sleep(POLL_MS);
		}
	}

	/** The elements in the DOM now, as [tag, attributes]. */
	private elements(): Array<[string, Record<string, string>]> {
		this.settle();
		if (this.current === "about:blank" || this.current === CHROME_ERROR_PAGE) return [];
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

async function navigate(path: string, hydrationDelayMs: number[], timeouts = TIMEOUTS, networkErrorVisits: number[] = []) {
	const page = new FakePage(hydrationDelayMs, networkErrorVisits);
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
	const { outcome, page, lines } = await navigate("/research/coverage", [NEVER, 20], SHORT_HYDRATION);
	assert(outcome.kind === "resolved", `the retry hydrated, so gotoStable must resolve: ${JSON.stringify(outcome)}`);
	assert(page.visits.length === 2, `expected one retry; gotoStable navigated ${page.visits.length} times`);
	assert(lines.length === 1 && lines[0].includes("/research/coverage"), `the retry must be printed: ${JSON.stringify(lines)}`);
}

// ─── (b5) #1856: a connection reset on attempt 1 costs one printed retry ─────
{
	const { outcome, page, lines, elapsedMs } = await navigate("/login", [NEVER, 20], TIMEOUTS, [0]);
	assert(
		outcome.kind === "resolved",
		`one connection reset must cost one retry, not the route; Chromium's error page interrupted the retry (#1856): ${JSON.stringify(outcome)}`,
	);
	assert(page.visits.length === 2, `expected one retry after the reset; gotoStable navigated ${page.visits.length} times`);
	assert(
		lines.length === 1 && lines[0].includes("/login") && lines[0].includes("net::ERR_CONNECTION_RESET"),
		`the reset must be printed with its route and its error: ${JSON.stringify(lines)}`,
	);
	assert(elapsedMs < TIMEOUTS.errorPageMs, `the error-page wait must end when the page commits, not at its bound: ${elapsedMs} ms`);
}

// ─── (b6) #1856: a reset on both attempts is an outage and still fails ──────
{
	const { outcome, page } = await navigate("/login", [NEVER, NEVER], TIMEOUTS, [0, 1]);
	assert(outcome.kind === "rejected", "gotoStable resolved although both attempts got a connection reset");
	assert(
		outcome.message.includes("attempt 1: page.goto: net::ERR_CONNECTION_RESET") &&
			outcome.message.includes("attempt 2: page.goto: net::ERR_CONNECTION_RESET"),
		`both attempts must reach the server and fail on their own reset, not on the error page: ${outcome.message}`,
	);
	assert(page.visits.length === 2, `an outage gets one retry; gotoStable navigated ${page.visits.length} times`);
}

// ─── (c) every page error fails its route, React #418 included (#1468) ───────
{
	const REACT_418 = "Minified React error #418; visit https://react.dev/errors/418?args[]=HTML&args[]= for the full message";
	const ledger = navigation.createPageErrorLedger();
	assert(
		!("ignoredReport" in ledger),
		"the ledger has an ignoredReport again: the walk must not ignore React #418, because it hides a hydration defect (#1468)",
	);

	ledger.startRoute("administrator", "/research/trace");
	ledger.record(REACT_418, "/research/trace");
	// assertTraceRendersDecisionInputs opens /research first, so the event names that page too.
	ledger.record(REACT_418, "/research");
	ledger.record("TypeError: Cannot read properties of undefined (reading 'value')", "/research/trace");
	assert(
		JSON.stringify(ledger.failures) ===
			JSON.stringify([
				REACT_418,
				`${REACT_418} (raised on /research)`,
				"TypeError: Cannot read properties of undefined (reading 'value')",
			]),
		`#418 must fail its route like every other page error: ${JSON.stringify(ledger.failures)}`,
	);

	// The walk reads the verdict of a route before it starts the next one.
	ledger.problems();
	ledger.startRoute("member", "/research/rankings");
	assert(ledger.failures.length === 0, `a new route starts with no page errors: ${JSON.stringify(ledger.failures)}`);
	ledger.record(REACT_418, "/research/rankings");
	assert(
		JSON.stringify(ledger.failures) === JSON.stringify([REACT_418]),
		`a #418 of the current route fails the route: ${JSON.stringify(ledger.failures)}`,
	);

	assert(
		JSON.stringify(ledger.problems()) === JSON.stringify([`pageerror: ${REACT_418}`]),
		`the route verdict must carry the #418 as a problem, so that the walk exits non-zero: ${JSON.stringify(ledger.problems())}`,
	);

	// A page error that comes after the verdict of a route is not lost: the next route reports it.
	ledger.record("Minified React error #423", "/research/rankings");
	ledger.startRoute("member", "/research/themes");
	assert(
		JSON.stringify(ledger.problems()) === JSON.stringify(["pageerror: Minified React error #423"]),
		`a late page error must reach the next verdict: ${JSON.stringify(ledger.problems())}`,
	);
	// After the last route of a pass, lateProblems() reports it. A read is reported once.
	ledger.record("Minified React error #425", "/research/themes");
	const late = ledger.lateProblems();
	assert(
		JSON.stringify(late) === JSON.stringify(["pageerror after the last route: Minified React error #425"]),
		`a page error after the last verdict must fail the pass: ${JSON.stringify(late)}`,
	);
	assert(ledger.lateProblems().length === 0, "a late page error is reported once");
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
