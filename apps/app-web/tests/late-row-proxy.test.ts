/**
 * #1468: the proxy that opens the hydration-race window in the CI walk.
 *
 * `e2e/hydration-race.mjs` visits pages through `e2e/late-row-proxy.mjs`. The proxy must hold back
 * exactly the rows that a host element references lazily. It must lose no byte. It must report a
 * page with nothing to hold back, because a proxy that holds nothing tests nothing.
 * This file proves the proxy on synthetic pages and on a real Next 16.4.2 page.
 * The browser half (RED on Next 15.5.18, GREEN on Next 16.4.2) is the walk itself.
 *
 * Run standalone: `bun run tests/late-row-proxy.test.ts`.
 */

import http from "node:http";
import { readFileSync } from "node:fs";
import * as proxy from "../e2e/late-row-proxy.mjs";

function assert(condition: unknown, message: string): asserts condition {
	if (!condition) throw new Error(message);
}

const scriptTag = (text: string) => `<script>self.__next_f.push([1,${JSON.stringify(text)}])</script>`;
const page = (...chunks: string[]) => `<html><body><main>m</main>${chunks.map(scriptTag).join("")}<script>tail</script></body></html>`;

const MODULES = '1:I[9766,[],""]\n3:I[8924,[],"Boundary"]\n';
/** The root layout: a header whose inner div holds the async chrome as the lazy child "$L2". */
const ROOT = '0:["$","header",null,{"children":["$","div",null,{"children":[["$","span",null,{"children":"x"}],"$L2"]}]}]\n';
const CHROME = '2:["$","div",null,{"children":"chrome"}]\n';

const flightText = (html: string) => proxy.readFlightScripts(html).map((script: { text: string }) => script.text).join("");

/** The rows of a document that has only newline-terminated rows, split by a plain newline split that
 * does not use `splitRows`, so that a defect of `splitRows` cannot hide itself. */
function rowsOf(html: string): string[] {
	return flightText(html)
		.split(/(?<=\n)/)
		.sort();
}

// ─── (a) a lazy child of a host element is held back, and the document loses nothing ───
{
	const html = page(MODULES + ROOT + CHROME);
	const split = proxy.splitAtLateRows(html);
	assert(split !== null, "a header whose div holds $L2 must have a late row");
	assert(JSON.stringify(split.rows) === '["2"]', `only row 2 is late: ${JSON.stringify(split.rows)}`);
	assert(split.early.includes("chrome") === false, "the early part must not carry the chrome row");
	assert(split.early.includes("$L2"), "the early part must still reference $L2");
	assert(split.late.startsWith("<script>self.__next_f.push([1,") && split.late.includes("chrome"), "the late part starts with the late row");
	assert(split.late.endsWith("<script>tail</script></body></html>"), "the late part ends with the rest of the document");
	assert(
		JSON.stringify(rowsOf(split.early + split.late)) === JSON.stringify(rowsOf(html)),
		"early + late must hold the same rows as the original document",
	);
}

// ─── (b) a lazy reference as a prop of a client component is safe: nothing to hold ───
{
	const html = page(MODULES + '0:["$","$L3",null,{"children":"$L2"}]\n' + CHROME);
	assert(proxy.splitAtLateRows(html) === null, "a client component handles its own suspension; the proxy must not hold it back");
}

// ─── (c) a single child that is not in an array, and a table row (`tr` under `tbody`) ───
{
	const single = page('0:["$","div",null,{"children":"$L2"}]\n' + CHROME);
	assert(proxy.splitAtLateRows(single)?.rows.join() === "2", "a host element with the one lazy child $L2 must hold row 2");
	const table =
		'0:["$","table",null,{"children":["$","tbody",null,{"children":["$L10","$L11"]}]}]\n' +
		'10:["$","tr","a",{"children":"a"}]\n11:["$","tr","b",{"children":"b"}]\n';
	assert(proxy.splitAtLateRows(page(table))?.rows.join() === "10,11", "both table rows are late rows");
}

// ─── (d) no flight script, and no lazy child, give null ───
{
	assert(proxy.splitAtLateRows("<html><body>plain</body></html>") === null, "a document without flight scripts has no late row");
	assert(proxy.splitAtLateRows(page('0:["$","div",null,{"children":"done"}]\n')) === null, "a document without a lazy child has no late row");
}

// ─── (e) the late script cannot end early; rows split over scripts and other tags are kept ───
{
	const hostile = '2:["$","div",null,{"children":"a</script><b>"}]\n';
	const html = page(MODULES + ROOT + hostile);
	const split = proxy.splitAtLateRows(html);
	assert(split !== null, "the hostile page has a late row");
	assert(!split.late.slice(0, split.late.indexOf("<script>tail")).includes("</script><b>"), "a </script> in a row must not end the script tag");
	assert(
		JSON.stringify(rowsOf(split.early + split.late)) === JSON.stringify(rowsOf(html)),
		"a row with </script> survives the split byte for byte",
	);

	// A row that starts in one script and ends in the next, with another tag between the scripts.
	const first = scriptTag(MODULES + '0:["$","div",null,{"children":["$","p",null,{"children":"$L');
	const second = scriptTag('2"}]}]\n' + CHROME);
	const doc = `<html><body>${first}<i>between</i>${second}</body></html>`;
	const joined = proxy.splitAtLateRows(doc);
	assert(joined !== null && joined.rows.join() === "2", `a row split over two scripts must be joined first: ${JSON.stringify(joined?.rows)}`);
	assert(joined.early.includes("<i>between</i>"), "a tag between flight scripts must stay in the early part");
}

// ─── (f) a real Next 16.4.2 page: AppChrome is held back and no row is lost ───
{
	const real = readFileSync(new URL("./helpers/next16-backtest-page.html", import.meta.url), "utf8");
	const split = proxy.splitAtLateRows(real);
	assert(split !== null, "the real page must have a late row: AppChrome is a lazy child of the header div");
	assert(split.rows.includes("2"), `row 2 (the AppChrome output) must be late: ${JSON.stringify(split.rows)}`);
	// The server markup also carries the chrome; only the flight rows move.
	assert(!flightText(split.early).includes("data-signed-in-as"), "the early flight rows must not carry the signed-in chrome");
	assert(flightText(split.late).includes("data-signed-in-as"), "the late flight rows must carry the signed-in chrome");
	assert(
		JSON.stringify(rowsOf(split.early + split.late)) === JSON.stringify(rowsOf(real)),
		"the real page must keep all its rows",
	);
	// The rows must cover the stream byte for byte, whatever the row format.
	const stream = flightText(real);
	assert(
		proxy.splitRows(stream).map((row: { text: string }) => row.text).join("") === stream,
		"splitRows must return every byte of the real stream",
	);
}

// ─── (f2) text rows have a byte length and no newline; nothing may be lost or swallowed ───
{
	// "é" has two bytes; the length 6 counts bytes, and the text holds a newline and a row-like line.
	const textRow = "7:T6,é\n9:x";
	const stream = ":HL[\"/a.css\",\"style\"]\n" + MODULES + ROOT + textRow + CHROME + "tail without newline";
	const rows = proxy.splitRows(stream);
	assert(rows.map((row: { text: string }) => row.text).join("") === stream, "splitRows must cover the stream byte for byte");
	assert(
		JSON.stringify(rows.map((row: { id: string }) => row.id)) === JSON.stringify(["", "1", "3", "0", "7", "2", ""]),
		`the hint row, the text row and the tail get their own entries: ${JSON.stringify(rows.map((row: { id: string }) => row.id))}`,
	);
	assert(rows[4].text === textRow, `the text row is exactly its header and its 6 bytes: ${JSON.stringify(rows[4].text)}`);

	// A text row right before the late row must not swallow it.
	const split = proxy.splitAtLateRows(page(MODULES + ROOT + textRow + CHROME));
	assert(split !== null && split.rows.join() === "2", `row 2 after a text row is still late: ${JSON.stringify(split?.rows)}`);
	assert(
		flightText(split.early + split.late).length === (MODULES + ROOT + textRow + CHROME).length,
		"a text row survives the split",
	);
	// A text row at the end, with no newline, must not be dropped.
	const tail = proxy.splitAtLateRows(page(MODULES + ROOT + CHROME + "8:T3,abc"));
	assert(tail !== null && flightText(tail.early).endsWith("8:T3,abc"), "a text row at the end stays in the early part");
	// A row with a bad length is kept as a line, not dropped.
	assert(proxy.splitRows("5:Tzz,abc\n").map((row: { text: string }) => row.text).join("") === "5:Tzz,abc\n", "a bad length must not lose bytes");
}

// ─── (g) the proxy: holds the late part for holdMs, loses no byte, and counts what it did not hold ───
{
	const held = page(MODULES + ROOT + CHROME);
	const plain = page('0:["$","div",null,{"children":"done"}]\n');
	const origin = http.createServer((request, response) => {
		if (request.url === "/held") response.writeHead(200, { "content-type": "text/html; charset=utf-8" }).end(held);
		else if (request.url === "/plain") response.writeHead(200, { "content-type": "text/html; charset=utf-8" }).end(plain);
		else if (request.url === "/rsc") response.writeHead(200, { "content-type": "text/html" }).end(held);
		else response.writeHead(200, { "content-type": "application/json" }).end('{"ok":true}');
	});
	await new Promise((resolve) => origin.listen(0, "127.0.0.1", () => resolve(undefined)));
	const originUrl = `http://127.0.0.1:${(origin.address() as import("node:net").AddressInfo).port}`;
	const HOLD_MS = 250;
	const front = await proxy.startLateRowProxy({ origin: originUrl, holdMs: HOLD_MS });

	function get(path: string, headers: Record<string, string> = {}) {
		return new Promise<{ body: string; firstAt: number; lastAt: number; first: string }>((resolve, reject) => {
			const started = Date.now();
			http
				.get(`${front.url}${path}`, { headers }, (answer) => {
					let body = "";
					let first = "";
					let firstAt = 0;
					answer.setEncoding("utf8");
					answer.on("data", (chunk) => {
						if (first === "") {
							first = chunk;
							firstAt = Date.now() - started;
						}
						body += chunk;
					});
					answer.on("end", () => resolve({ body, first, firstAt, lastAt: Date.now() - started }));
				})
				.on("error", reject);
		});
	}

	try {
		const expected = proxy.splitAtLateRows(held);
		assert(expected !== null, "the held page must have a late row");
		const result = await get("/held");
		assert(result.body === expected.early + expected.late, "the proxy must deliver exactly early + late");
		assert(!result.first.includes("chrome"), "the first bytes must not carry the late row");
		assert(
			result.lastAt - result.firstAt >= HOLD_MS - 40,
			`the late part must arrive about ${HOLD_MS} ms after the early part: ${result.lastAt - result.firstAt} ms`,
		);

		const json = await get("/api");
		assert(json.body === '{"ok":true}' && json.lastAt < HOLD_MS, `a JSON answer passes through at once: ${json.body} after ${json.lastAt} ms`);

		const rsc = await get("/rsc", { rsc: "1" });
		assert(rsc.body === held && rsc.lastAt < HOLD_MS, "a request with the rsc header passes through at once");

		assert(JSON.stringify(front.stats.held) === '["/held"]', `one document was held: ${JSON.stringify(front.stats.held)}`);
		assert(front.stats.unheld.length === 0, `no document is unheld yet: ${JSON.stringify(front.stats.unheld)}`);
		const unheld = await get("/plain");
		assert(unheld.body === plain, "a page without a late row is delivered unchanged");
		assert(JSON.stringify(front.stats.unheld) === '["/plain"]', `the proxy must name the unheld page: ${JSON.stringify(front.stats.unheld)}`);
	} finally {
		await front.close();
		await new Promise((resolve) => origin.close(() => resolve(undefined)));
	}
}

console.log("#1468 late-row proxy passed");
