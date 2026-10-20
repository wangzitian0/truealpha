/**
 * #811: `SignOutButton` says when its click handler is live.
 *
 * The sign-out walk waits for `[data-sign-out][data-hydrated="true"]` before it
 * clicks. That wait means something only if both of these hold:
 *   1. the server render does NOT carry the marker. If it did, the pre-hydration
 *      DOM would match, the walk would click an inert button again, and
 *      the false "cookie survived" claim would come back;
 *   2. the mounted component DOES carry it. Otherwise every walk would fail
 *      with "never hydrated".
 *
 * (1) uses React's server renderer, the same one Next.js uses for the HTML the
 * walk loads first. (2) mounts the component with React's client renderer
 * into the small in-memory DOM of `tests/helpers/fake-dom.ts`. The
 * real-browser version of (2) is the walk itself in ci-web's browser job. It
 * fails with "never hydrated" if the marker never shows up.
 *
 * Run standalone: `bun run tests/sign-out-button.test.ts`.
 */

import { act, createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { HYDRATED_CONTROL } from "../e2e/sign-out-journey.mjs";
import { SignOutButton } from "../src/components/sign-out-button";
import { FakeElement, installFakeDom } from "./helpers/fake-dom";

function assert(condition: unknown, message: string): asserts condition {
	if (!condition) throw new Error(message);
}

// The walk's selector names exactly the two attributes asserted below.
assert(
	HYDRATED_CONTROL === '[data-sign-out][data-hydrated="true"]',
	`the walk waits for ${HYDRATED_CONTROL}; this test asserts data-sign-out + data-hydrated="true" — update both together`,
);

// ─── (1) the server render has the control and no hydration marker ──────────
{
	const html = renderToStaticMarkup(createElement(SignOutButton));
	assert(html.includes('data-sign-out="true"'), `the server render lost the control's selector: ${html}`);
	assert(
		!html.includes("data-hydrated"),
		`the server render already says data-hydrated, so the walk's wait matches the inert pre-hydration button (#811): ${html}`,
	);
}

// ─── (2) after mount the marker is present ───────────────────────────────────
{
	const fakeDocument = installFakeDom();
	// Imported only now: react-dom/client reads the globals above when it loads.
	const { createRoot } = await import("react-dom/client");
	const container = fakeDocument.createElement("div");
	const root = createRoot(container as unknown as Element);
	// `act` flushes the render and its effects, which is what mounting means here.
	await act(async () => {
		root.render(createElement(SignOutButton));
	});
	const button = container.childNodes[0];
	assert(button instanceof FakeElement && button.tagName === "BUTTON", "SignOutButton did not mount a <button>");
	assert(button.getAttribute("data-sign-out") === "true", "the mounted control lost its selector");
	assert(
		button.getAttribute("data-hydrated") === "true",
		`the mounted control never says data-hydrated="true", so every walk fails with "never hydrated" (#811); attributes: ${JSON.stringify([...button.attributes])}`,
	);
	await act(async () => {
		root.unmount();
	});
}

console.log("#811 sign-out button hydration marker passed");
