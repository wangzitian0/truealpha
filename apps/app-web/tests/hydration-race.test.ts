/**
 * #1468: the verdicts of the hydration-race check of the CI walk.
 *
 * `runHydrationRace` of `e2e/hydration-race.mjs` visits routes through the late-row proxy and
 * turns what it sees into problems. This drives it with a fake browser and a real proxy in front
 * of a local server. The fake navigation makes a real HTTP request through the proxy, so the
 * proxy's counts are real; only the browser's answers (page errors, retries, throws) are scripted.
 * Each guard of the runner has a scenario here that fails when the guard is deleted.
 * The browser half (RED on Next 15.5.18, GREEN on Next 16.4.2) is the walk itself.
 *
 * Run standalone: `bun run tests/hydration-race.test.ts`.
 */

import http from "node:http";
import { runHydrationRace } from "../e2e/hydration-race.mjs";

function assert(condition: unknown, message: string): asserts condition {
	if (!condition) throw new Error(message);
}

const scriptTag = (text: string) => `<script>self.__next_f.push([1,${JSON.stringify(text)}])</script>`;
const page = (rows: string) => `<html><body><main>m</main>${scriptTag(rows)}</body></html>`;
const WITH_LATE_ROW =
	page('0:["$","header",null,{"children":["$","div",null,{"children":"$L2"}]}]\n2:["$","div",null,{"children":"chrome"}]\n');
const WITHOUT_LATE_ROW = page('0:["$","div",null,{"children":"done"}]\n');

const ROUTES = ["/a", "/b"];
const VISITS = 2;
const HOLD_MS = 20;

type Handler = (error: Error) => void;
type Behaviour = (route: string, visit: number, emit: (message: string) => void, log: (line: string) => void) => void | Promise<void>;

class FakePage {
	handlers: Handler[] = [];
	on(event: string, handler: Handler) {
		if (event === "pageerror") this.handlers.push(handler);
	}
	async waitForTimeout() {}
	async close() {}
}

function get(url: string): Promise<string> {
	return new Promise((resolve, reject) => {
		http
			.get(url, (answer) => {
				let body = "";
				answer.setEncoding("utf8");
				answer.on("data", (chunk) => (body += chunk));
				answer.on("end", () => resolve(body));
			})
			.on("error", reject);
	});
}

async function run(options: { document?: string; loginStatus?: number; behaviour?: Behaviour; bypass?: (route: string, visit: number) => boolean }) {
	const document = options.document ?? WITH_LATE_ROW;
	const origin = http.createServer((_request, response) => {
		response.writeHead(200, { "content-type": "text/html; charset=utf-8" }).end(document);
	});
	await new Promise((resolve) => origin.listen(0, "127.0.0.1", () => resolve(undefined)));
	const originUrl = `http://127.0.0.1:${(origin.address() as import("node:net").AddressInfo).port}`;
	const seen = new Map<string, number>();
	const browser = {
		newContext: async () => ({
			request: { post: async () => ({ status: () => options.loginStatus ?? 200 }) },
			newPage: async () => new FakePage(),
			close: async () => {},
		}),
	};
	const navigate = async (fake: FakePage, url: string, { log }: { log: (line: string) => void }) => {
		const route = new URL(url).pathname;
		const visit = (seen.get(route) ?? 0) + 1;
		seen.set(route, visit);
		// A visit that skips the proxy reaches the server directly.
		await get(options.bypass?.(route, visit) ? `${originUrl}${route}` : url);
		await options.behaviour?.(route, visit, (message) => fake.handlers.forEach((handler) => handler(new Error(message))), log);
	};
	try {
		return await runHydrationRace({
			browser,
			base: originUrl,
			email: "a@e2e.invalid",
			password: "x",
			routes: ROUTES,
			visitsPerRoute: VISITS,
			holdMs: HOLD_MS,
			navigate,
		});
	} finally {
		await new Promise((resolve) => origin.close(() => resolve(undefined)));
	}
}

// ─── (a) healthy: every visit is held, no page error, no problem ───
{
	const result = await run({});
	assert(result.problems.length === 0, `a healthy run must have no problem: ${JSON.stringify(result.problems)}`);
	assert(result.visits === ROUTES.length * VISITS && result.held === result.visits, `every visit is held: ${JSON.stringify(result)}`);
}

// ─── (b) a page error of one visit fails the run and names the route and the visit ───
{
	const result = await run({
		behaviour: (route, visit, emit) => {
			if (route === "/b" && visit === 2) emit("Minified React error #418; visit https://react.dev/errors/418");
		},
	});
	assert(result.problems.length === 1, `one page error gives one problem: ${JSON.stringify(result.problems)}`);
	assert(
		result.problems[0].startsWith("[hydration-race] /b visit 2: Minified React error #418"),
		`the problem names the route and the visit: ${result.problems[0]}`,
	);
}

// ─── (c) a retry of the navigation fails the run: a hydration hang must not pass on the second try ───
{
	const result = await run({
		behaviour: (route, visit, _emit, log) => {
			if (route === "/a" && visit === 1) log("  retry /a: attempt 1 failed: did not hydrate");
		},
	});
	assert(
		result.problems.length === 1 && result.problems[0].includes("/a visit 1: navigation retried: retry /a: attempt 1 failed"),
		`a retry is a problem: ${JSON.stringify(result.problems)}`,
	);
}

// ─── (d) a navigation that throws is a problem ───
{
	const result = await run({
		behaviour: (route, visit) => {
			if (route === "/a" && visit === 2) throw new Error("gotoStable: /a failed twice. attempt 1: x");
		},
	});
	assert(
		result.problems.length === 1 && result.problems[0].includes("/a visit 2: did not hydrate: gotoStable: /a failed twice"),
		`a throw is a problem: ${JSON.stringify(result.problems)}`,
	);
}

// ─── (e) a failed login stops the run with its status ───
{
	const result = await run({ loginStatus: 429 });
	assert(
		JSON.stringify(result.problems) === '["[hydration-race] login answered HTTP 429"]' && result.visits === 0,
		`a failed login is the only problem: ${JSON.stringify(result)}`,
	);
}

// ─── (f) GREEN-WHILE-EMPTY: pages without a late row make the run fail, not pass ───
{
	const result = await run({ document: WITHOUT_LATE_ROW });
	assert(result.problems.length === ROUTES.length, `one problem per route: ${JSON.stringify(result.problems)}`);
	for (const route of ROUTES) {
		const problem = result.problems.find((text: string) => text.startsWith(`[hydration-race] ${route}:`));
		assert(problem !== undefined && problem.includes(`for 0 of ${VISITS} visits`), `route ${route} names its count: ${problem}`);
		assert(problem.includes("delete this check"), `the problem tells the maintainer what to do: ${problem}`);
	}
}

// ─── (g) a visit that did not pass the proxy is counted against its route ───
{
	const result = await run({ bypass: (route, visit) => route === "/b" && visit === 2 });
	assert(result.problems.length === 1, `one route is short of held documents: ${JSON.stringify(result.problems)}`);
	assert(
		result.problems[0].startsWith("[hydration-race] /b: the proxy held a late flight row for 1 of 2 visits") &&
			result.problems[0].includes("reached the server without the proxy"),
		`a route with a missing held document gets its own advice: ${result.problems[0]}`,
	);
}

console.log("#1468 hydration-race verdicts passed");
