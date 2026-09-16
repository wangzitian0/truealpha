/**
 * #811: the sign-out journey reports the cause it saw, and it never makes the
 * cookie claim because a click was lost.
 *
 * `e2e/sign-out-journey.mjs` runs against a real browser only in ci-web's
 * browser job and after each deploy, and a race is the kind of thing a real
 * browser shows at random. This drives the same functions against a fake
 * browser. The fake models the one fact behind #811: the control is in the DOM
 * before its click handler exists, and a click in that window does nothing.
 * Each scenario breaks one property and checks that the journey names that
 * property and nothing else.
 *
 * The fake reads selectors by parsing them against the element's attributes.
 * It does not import the journey's selector constants, so a journey that stops
 * waiting for `data-hydrated` clicks the inert button here and fails.
 *
 * Run standalone: `bun run tests/sign-out-journey.test.ts`.
 */

import { SESSION_COOKIE, walkSignOutJourney } from "../e2e/sign-out-journey.mjs";

function assert(condition: unknown, message: string): asserts condition {
	if (!condition) throw new Error(message);
}

const BASE = "http://walk.invalid";
const LOGOUT = "/api/auth/logout";
const POLL_MS = 5;
// Small enough to run fast, and far enough apart that timer jitter cannot turn
// a pass into a fail: hydration happens at 60 ms, and the journey waits up to 1 s.
const TIMEOUTS = { hydrationMs: 1000, signOutMs: 400, clickMs: 200 };
const HYDRATION_DELAY_MS = 60;
// A cold deploy: hydration still inside the journey's bound, but slower than
// the sign-out window.
const COLD_HYDRATION_DELAY_MS = 650;

type LogoutBehaviour = "clears" | "keeps" | "network-error" | "hangs" | { status: number };

interface Scenario {
	/** ms from navigation until React has hydrated the control; Infinity = never */
	hydrationDelayMs: number;
	controlRendered: boolean;
	/** the hydrated control's onClick really POSTs the logout */
	clickWired: boolean;
	logout: LogoutBehaviour;
	/** protected routes send a request without a session to /login */
	gate: boolean;
	/** a context's first protected navigation is sent to /login even with a session */
	bounceFreshSession: boolean;
}

const HEALTHY: Scenario = {
	hydrationDelayMs: HYDRATION_DELAY_MS,
	controlRendered: true,
	clickWired: true,
	logout: "clears",
	gate: true,
	bounceFreshSession: false,
};

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

class TimeoutError extends Error {}

async function pollUntil(condition: () => boolean, timeoutMs: number, what: string): Promise<void> {
	const deadline = Date.now() + timeoutMs;
	while (!condition()) {
		if (Date.now() >= deadline) throw new TimeoutError(`Timeout ${timeoutMs}ms exceeded waiting for ${what}`);
		await sleep(POLL_MS);
	}
}

/** `[a]`, `[a="v"]`, repeated. Anything else is a selector the fake does not
 * understand, and the test fails loudly rather than guessing. */
function matches(selector: string, attributes: Record<string, string> | null): boolean {
	const parts = [...selector.matchAll(/\[([\w-]+)(?:="([^"]*)")?\]/g)];
	assert(parts.map((m) => m[0]).join("") === selector, `the fake cannot read selector ${selector}`);
	if (!attributes) return false;
	return parts.every(([, name, value]) => name in attributes && (value === undefined || attributes[name] === value));
}

interface Clicked {
	hydrated: boolean;
}

interface StorageState {
	cookies: Array<{ name: string; value: string; expires: number }>;
}

class World {
	readonly clicks: Clicked[] = [];
	logins = 0;
	contextsOpened = 0;
	contextsClosed = 0;
	constructor(readonly scenario: Scenario) {}
}

class FakeResponse {
	constructor(private readonly code: number) {}
	status(): number {
		return this.code;
	}
	ok(): boolean {
		return this.code >= 200 && this.code < 300;
	}
}

class FakeRequest {
	private failed: string | null = null;
	private readonly answered: Promise<FakeResponse | null>;
	constructor(
		private readonly path: string,
		outcome: Promise<FakeResponse>,
	) {
		this.answered = outcome.catch((error: Error) => {
			this.failed = error.message;
			return null;
		});
	}
	method(): string {
		return "POST";
	}
	url(): string {
		return `${BASE}${this.path}`;
	}
	response(): Promise<FakeResponse | null> {
		return this.answered;
	}
	failure(): { errorText: string } | null {
		return this.failed === null ? null : { errorText: this.failed };
	}
}

class FakeContext {
	private session: string | null = null;
	private protectedVisits = 0;
	readonly request = {
		post: (url: string, _options?: unknown): Promise<FakeResponse> => this.serve(new URL(url).pathname),
	};
	constructor(
		readonly world: World,
		storageState?: StorageState,
	) {
		world.contextsOpened += 1;
		this.session = storageState?.cookies.find((cookie) => cookie.name === SESSION_COOKIE)?.value ?? null;
	}

	/** The server: login sets the cookie, logout behaves as the scenario says. */
	serve(path: string): Promise<FakeResponse> {
		if (path === "/api/auth/login") {
			this.world.logins += 1;
			this.session = "signed-token";
			return Promise.resolve(new FakeResponse(200));
		}
		assert(path === LOGOUT, `the fake server has no route ${path}`);
		const behaviour = this.world.scenario.logout;
		if (behaviour === "clears") {
			this.session = null;
			return Promise.resolve(new FakeResponse(200));
		}
		if (behaviour === "keeps") return Promise.resolve(new FakeResponse(200));
		if (behaviour === "network-error") return Promise.reject(new Error("net::ERR_CONNECTION_RESET"));
		if (behaviour === "hangs") {
			// Playwright's request API gives up on its own; a page's fetch just waits.
			return sleep(TIMEOUTS.signOutMs * 3).then(() => {
				throw new Error("Request timed out");
			});
		}
		return Promise.resolve(new FakeResponse(behaviour.status));
	}

	/** Where a navigation lands, as the app's server-side gates decide it. */
	land(path: string): string {
		if (path !== "/research") return path;
		this.protectedVisits += 1;
		const { gate, bounceFreshSession } = this.world.scenario;
		if ((gate && this.session === null) || (bounceFreshSession && this.protectedVisits === 1)) {
			return "/login?from=%2Fresearch";
		}
		return path;
	}

	hasSession(): boolean {
		return this.session !== null;
	}

	async cookies(...urls: string[]) {
		// Playwright's URL filter drops a Secure cookie for http://127.0.0.1, which is
		// where CI serves the production build, so a filtered read finds nothing.
		assert(urls.length === 0, `the journey filtered the jar by ${urls.join(", ")}; read the whole jar`);
		return this.session === null ? [] : [{ name: SESSION_COOKIE, value: this.session, expires: -1 }];
	}

	async storageState(): Promise<StorageState> {
		return { cookies: await this.cookies() };
	}

	async newPage(): Promise<FakePage> {
		return new FakePage(this);
	}

	async close(): Promise<void> {
		this.world.contextsClosed += 1;
	}
}

class FakePage {
	private current = "about:blank";
	private hydratedAt = Number.POSITIVE_INFINITY;
	private hasControl = false;
	private readonly requestWaiters: Array<(request: FakeRequest) => void> = [];
	constructor(private readonly context: FakeContext) {}

	url(): string {
		return this.current;
	}

	async goto(url: string): Promise<void> {
		const landed = this.context.land(new URL(url).pathname);
		this.current = `${BASE}${landed}`;
		// The chrome renders the control for any request that carries a session.
		// React attaches its handler later.
		this.hasControl = this.context.world.scenario.controlRendered && this.context.hasSession();
		this.hydratedAt = Date.now() + this.context.world.scenario.hydrationDelayMs;
	}

	private hydrated(): boolean {
		return Date.now() >= this.hydratedAt;
	}

	/** The control's attributes right now, or null when it is not in the DOM. */
	controlAttributes(): Record<string, string> | null {
		if (!this.hasControl) return null;
		return this.hydrated() ? { "data-sign-out": "true", "data-hydrated": "true" } : { "data-sign-out": "true" };
	}

	locator(selector: string): FakeLocator {
		return new FakeLocator(this, selector);
	}

	async waitForSelector(selector: string, options: { state?: string; timeout: number }): Promise<void> {
		assert(options.state === "attached", "the journey waits for the marker to be attached, not for visibility");
		await pollUntil(() => matches(selector, this.controlAttributes()), options.timeout, selector);
	}

	waitForRequest(predicate: (request: FakeRequest) => boolean, options: { timeout: number }): Promise<FakeRequest> {
		return new Promise((resolve, reject) => {
			const timer = setTimeout(() => reject(new TimeoutError("waitForRequest timed out")), options.timeout);
			this.requestWaiters.push((request) => {
				if (!predicate(request)) return;
				clearTimeout(timer);
				resolve(request);
			});
		});
	}

	/** Playwright resolves at the requested lifecycle point, and its default is
	 * `load`. `load` waits for the page's scripts, and in this fake the scripts
	 * arriving is the moment the page hydrates. */
	async waitForURL(predicate: (url: URL) => boolean, options: { timeout: number; waitUntil?: string }): Promise<void> {
		const reachedLifecycle = () => options.waitUntil === "commit" || this.hydrated();
		await pollUntil(() => predicate(new URL(this.current)) && reachedLifecycle(), options.timeout, "url");
	}

	/** What `SignOutButton` does once React has attached its handler. */
	click(): void {
		const hydrated = this.hydrated();
		this.context.world.clicks.push({ hydrated });
		if (!hydrated || !this.context.world.scenario.clickWired) return;
		const request = new FakeRequest(LOGOUT, this.context.serve(LOGOUT));
		for (const waiter of this.requestWaiters) waiter(request);
		// `await fetch(...)` then, in `finally`, a full navigation to /login.
		void request.response().then(() => this.goto(`${BASE}/login`));
	}
}

class FakeLocator {
	constructor(
		private readonly page: FakePage,
		private readonly selector: string,
	) {}
	async count(): Promise<number> {
		return matches(this.selector, this.page.controlAttributes()) ? 1 : 0;
	}
	first(): FakeLocator {
		return this;
	}
	async click(options: { timeout: number }): Promise<void> {
		// Playwright waits for the element to exist; it does not wait for a handler.
		await pollUntil(() => matches(this.selector, this.page.controlAttributes()), options.timeout, this.selector);
		this.page.click();
	}
}

class FakeBrowser {
	constructor(private readonly world: World) {}
	async newContext(options?: { storageState?: StorageState }): Promise<FakeContext> {
		return new FakeContext(this.world, options?.storageState);
	}
}

/** `gotoStable`'s contract as the journey sees it: the page has navigated. */
const gotoStable = (page: FakePage, url: string) => page.goto(url);

async function walk(overrides: Partial<Scenario>) {
	const world = new World({ ...HEALTHY, ...overrides });
	const lines: string[] = [];
	const failures = await walkSignOutJourney(new FakeBrowser(world), {
		base: BASE,
		email: "walker@e2e.invalid",
		password: "not-a-secret",
		gotoStable,
		timeouts: TIMEOUTS,
		log: (line: string) => lines.push(line),
	});
	const line = (label: string) => {
		const found = lines.filter((l) => l.includes(`[${label}]`));
		assert(found.length === 1, `expected one [${label}] line, got ${JSON.stringify(lines)}`);
		return found[0];
	};
	const control = line("sign-out control");
	const endpoint = line("sign-out endpoint");
	// One context to log in, then one per half. A half that reused another's
	// context would start from whatever that half did to the cookie.
	assert(world.contextsOpened === 3, `expected a login context and one context per half; ${world.contextsOpened} opened`);
	assert(world.contextsClosed === 3, `${world.contextsClosed} of 3 contexts closed`);
	// The login route allows 10 attempts per IP per 5 minutes, and the route passes spend two.
	assert(world.logins === 1, `the journey logged in ${world.logins} times; one login is its budget`);
	return { failures, world, control, endpoint };
}

const passed = (line: string) => line.startsWith("ok   ");
const failedWith = (line: string, label: string, message: string) => line === `FAIL [${label}] — ${message}`;

// ─── healthy, with hydration arriving after the page is up ───────────────────
{
	const { failures, world, control, endpoint } = await walk({});
	assert(failures === 0, `a healthy deployment failed: ${control} / ${endpoint}`);
	assert(passed(control) && passed(endpoint), `a healthy deployment printed ${control} / ${endpoint}`);
	assert(world.clicks.length === 1, `one click is enough once hydrated; ${world.clicks.length} were made`);
	assert(world.clicks[0].hydrated, "the journey clicked before the control hydrated (#811)");
}

// ─── a cold deploy: slow hydration, and /login's scripts load just as slowly ──
{
	const { failures, world, control, endpoint } = await walk({ hydrationDelayMs: COLD_HYDRATION_DELAY_MS });
	assert(failures === 0, `a slow but healthy deployment failed: ${control} / ${endpoint}`);
	assert(world.clicks.length === 1 && world.clicks[0].hydrated, "one click, after hydration");
}

// ─── never hydrates: its own message, never the cookie claim ─────────────────
{
	const { failures, world, control, endpoint } = await walk({ hydrationDelayMs: Number.POSITIVE_INFINITY });
	assert(
		failedWith(control, "sign-out control", "the sign-out control never hydrated within 1 s (client bundle did not run)"),
		`an unhydrated control must be reported as that: ${control}`,
	);
	assert(!/cookie/i.test(control), `a lost click made a cookie claim again (#811): ${control}`);
	assert(world.clicks.length === 0, "the journey clicked a control that never hydrated");
	assert(passed(endpoint), `the endpoint half must not depend on the click: ${endpoint}`);
	assert(failures === 1, `one half failed; the journey counted ${failures}`);
}

// ─── never hydrates AND the endpoint is broken: two causes, two messages ─────
{
	const { failures, control, endpoint } = await walk({ hydrationDelayMs: Number.POSITIVE_INFINITY, logout: "keeps" });
	assert(control.includes("never hydrated"), `control half: ${control}`);
	assert(
		failedWith(
			endpoint,
			"sign-out endpoint",
			"logout endpoint did not clear the cookie; protected route still rendered after logout (/research)",
		),
		`a broken endpoint must be reported even when the click could not be tried: ${endpoint}`,
	);
	assert(failures === 2, `two halves failed; the journey counted ${failures}`);
}

// ─── hydrated, but the click is not wired to the endpoint ────────────────────
{
	const { control, endpoint } = await walk({ clickWired: false });
	assert(
		failedWith(control, "sign-out control", `control present but did not sign out (no POST ${LOGOUT} within 0.4 s of the click)`),
		`an unwired control: ${control}`,
	);
	assert(passed(endpoint), `the endpoint works in this scenario: ${endpoint}`);
}

// ─── the endpoint answers 200 and leaves the cookie ──────────────────────────
{
	const { control, endpoint } = await walk({ logout: "keeps" });
	assert(
		failedWith(control, "sign-out control", `control present but did not sign out (${SESSION_COOKIE} is still set after the click)`),
		`a click whose logout keeps the cookie: ${control}`,
	);
	assert(
		failedWith(
			endpoint,
			"sign-out endpoint",
			"logout endpoint did not clear the cookie; protected route still rendered after logout (/research)",
		),
		`an endpoint that keeps the cookie: ${endpoint}`,
	);
}

// ─── the endpoint fails at the proxy (v0.0.73's likely shape) ────────────────
{
	const { control, endpoint } = await walk({ logout: { status: 502 } });
	assert(
		failedWith(control, "sign-out control", `control present but did not sign out (POST ${LOGOUT} answered 502)`),
		`a 502 behind the click: ${control}`,
	);
	assert(
		failedWith(
			endpoint,
			"sign-out endpoint",
			"logout endpoint answered 502; logout endpoint did not clear the cookie; protected route still rendered after logout (/research)",
		),
		`a 502 from the endpoint: ${endpoint}`,
	);
}
{
	const { control, endpoint } = await walk({ logout: "network-error" });
	assert(
		failedWith(control, "sign-out control", `control present but did not sign out (POST ${LOGOUT} failed: net::ERR_CONNECTION_RESET)`),
		`a reset behind the click: ${control}`,
	);
	assert(endpoint.includes("logout endpoint could not be reached (net::ERR_CONNECTION_RESET)"), `a reset: ${endpoint}`);
	assert(endpoint.includes("logout endpoint did not clear the cookie"), `a reset leaves the cookie: ${endpoint}`);
}
{
	const { control } = await walk({ logout: "hangs" });
	assert(
		failedWith(control, "sign-out control", `control present but did not sign out (POST ${LOGOUT} unanswered after 0.4 s)`),
		`a logout that never answers: ${control}`,
	);
}

// ─── the cookie is cleared but the route does not check it ───────────────────
{
	const { control, endpoint } = await walk({ gate: false });
	assert(passed(control), `the click path works in this scenario: ${control}`);
	assert(
		failedWith(endpoint, "sign-out endpoint", "protected route still rendered after logout (/research)"),
		`an ungated route: ${endpoint}`,
	);
}

// ─── no control at all ───────────────────────────────────────────────────────
{
	const { control, endpoint } = await walk({ controlRendered: false });
	assert(failedWith(control, "sign-out control", "no sign-out control on an authenticated page"), `no control: ${control}`);
	assert(passed(endpoint), `the endpoint works in this scenario: ${endpoint}`);
}

// ─── the fresh session is bounced before anything is clicked ─────────────────
{
	const { world, control, endpoint } = await walk({ bounceFreshSession: true });
	assert(
		failedWith(control, "sign-out control", "a fresh session was sent from /research to /login before sign-out was tried"),
		`a bounced fresh session: ${control}`,
	);
	assert(world.clicks.length === 0, "the journey clicked on a page it had been bounced to");
	assert(passed(endpoint), `the endpoint works in this scenario: ${endpoint}`);
}

console.log("#811 sign-out journey passed");
