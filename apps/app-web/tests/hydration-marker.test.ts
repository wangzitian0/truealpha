/**
 * #1412 W2: the root layout marks the document as hydrated, on every page.
 *
 * `gotoStable` (e2e/navigation.mjs) waits for `html[data-hydrated="true"]`
 * after each walk navigation. That wait means something only if all of these
 * hold:
 *   1. the root layout renders `HydrationMarker`, so every page carries it;
 *   2. the server HTML of the root layout does NOT carry `data-hydrated`.
 *      If it did, the wait would match the page before React hydrated it;
 *   3. a render alone does not set the attribute. Only the effect, which runs
 *      after React has committed the hydrated tree, sets it;
 *   4. after mount the document element carries the attribute that the walk's
 *      selector reads. Otherwise every walk fails on its first route.
 *
 * (2) renders the real root layout with the real top bar of a signed-in
 * administrator, with React's server renderer. (3) and (4) use the in-memory
 * DOM of `tests/helpers/fake-dom.ts`. The real-browser check is the walk in
 * ci-web's browser job: it fails with "did not hydrate" if the marker never
 * shows up.
 *
 * Run standalone: `bun run tests/hydration-marker.test.ts`.
 */

import { act, createElement, isValidElement, type ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { HYDRATED_DOCUMENT } from "../e2e/navigation.mjs";
import { HydrationMarker } from "../src/components/hydration-marker";
import { installFakeDom } from "./helpers/fake-dom";

function assert(condition: unknown, message: string): asserts condition {
	if (!condition) throw new Error(message);
}

const bunTestModuleId = "bun:test";
const { mock } = (await import(bunTestModuleId)) as {
	mock: { module: (id: string, factory: () => Record<string, unknown>) => void };
};

// The top bar is an async server component, and React 18's server renderer
// cannot await it. Resolve it once with a signed-in administrator, then give
// the layout the resolved element.
mock.module("@/server/auth/request-context", () => ({
	getServerPrincipal: async () => ({
		principalKind: "administrator" as const,
		context: {
			contextId: "ctx:hydration-marker",
			principalId: "principal:e2e:admin",
			tenantId: "tenant:truealpha",
			sessionId: "sess:hydration-marker",
			authenticationMethod: "session" as const,
			issuedAt: "2026-10-20T00:00:00Z",
			expiresAt: "2026-10-20T12:00:00Z",
		},
	}),
}));
const { AppChrome } = await import("../src/components/app-chrome");
const chrome = await AppChrome();
assert(chrome !== null, "AppChrome rendered nothing for a signed-in administrator");
mock.module("@/components/app-chrome", () => ({ AppChrome: () => chrome }));
const { default: RootLayout } = await import("../src/app/layout");

// The walk's selector names exactly the attribute and the value asserted below.
assert(
	HYDRATED_DOCUMENT === 'html[data-hydrated="true"]',
	`the walk waits for ${HYDRATED_DOCUMENT}; this test asserts data-hydrated="true" on <html> — update both together`,
);

/** True when `type` is rendered somewhere in the element tree under `node`. */
function renders(node: ReactNode, type: unknown): boolean {
	if (Array.isArray(node)) return node.some((child) => renders(child, type));
	if (!isValidElement(node)) return false;
	if (node.type === type) return true;
	return renders((node.props as { children?: ReactNode }).children, type);
}

const page = createElement("p", null, "page");

// ─── (1) the root layout renders the marker, so every page has it ───────────
assert(
	renders(RootLayout({ children: page }), HydrationMarker),
	"the root layout does not render <HydrationMarker />, so no page ever sets html[data-hydrated] and every walk fails",
);

// ─── (2) the server HTML has no marker ───────────────────────────────────────
// `document` does not exist yet, as on the server. A component that touched it
// during render would throw here.
{
	const html = renderToStaticMarkup(createElement(RootLayout, { children: page }));
	assert(html.startsWith("<html"), `the root layout no longer renders <html>: ${html.slice(0, 80)}`);
	// The real top bar is in this HTML, so the check below covers its markup too.
	assert(html.includes('data-sign-out="true"') && html.includes("<p>page</p>"), `the layout lost the top bar or the page: ${html}`);
	assert(
		!html.includes("data-hydrated"),
		`the server HTML already says data-hydrated, so the walk's wait matches a page that React has not hydrated (#1412): ${html}`,
	);
}

// ─── (3) and (4) a render alone does not set it; the mounted effect does ─────
{
	const fakeDocument = installFakeDom();
	renderToStaticMarkup(createElement(HydrationMarker));
	assert(
		fakeDocument.documentElement.getAttribute("data-hydrated") === null,
		"HydrationMarker sets data-hydrated during render, before React has hydrated anything; set it in an effect",
	);

	// Imported only now: react-dom/client reads the globals above when it loads.
	const { createRoot } = await import("react-dom/client");
	const container = fakeDocument.createElement("div");
	const root = createRoot(container as unknown as Element);
	// `act` flushes the render and its effects, which is what mounting means here.
	await act(async () => {
		root.render(createElement(HydrationMarker));
	});
	assert(container.childNodes.length === 0, "HydrationMarker must render no element of its own");
	assert(
		fakeDocument.documentElement.getAttribute("data-hydrated") === "true",
		`after mount <html> does not say data-hydrated="true", so the walk's wait for ${HYDRATED_DOCUMENT} times out on every route; attributes: ${JSON.stringify([...fakeDocument.documentElement.attributes])}`,
	);
	await act(async () => {
		root.unmount();
	});
}

console.log("#1412 hydration marker passed");
