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
 * into a small in-memory DOM that has only what React touches for one button.
 * The app has no DOM library, and this one check does not justify adding one to
 * the lockfile. The real-browser version of (2) is the walk itself in ci-web's
 * browser job. It fails with "never hydrated" if the marker never shows up.
 *
 * Run standalone: `bun run tests/sign-out-button.test.ts`.
 */

import { act, createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { HYDRATED_CONTROL } from "../e2e/sign-out-journey.mjs";
import { SignOutButton } from "../src/components/sign-out-button";

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
class FakeNode {
	childNodes: FakeNode[] = [];
	parentNode: FakeNode | null = null;
	ownerDocument: FakeDocument | null = null;
	nodeValue: string | null = null;
	constructor(
		public nodeType: number,
		public nodeName: string,
	) {}
	get firstChild(): FakeNode | null {
		return this.childNodes[0] ?? null;
	}
	// DOM semantics for the three tree edits. Inserting a node moves it out of its
	// current parent. A null reference node means "append". A reference or removed
	// node that is not a child is a NotFoundError, not a silent splice(-1).
	private static detach(child: FakeNode): void {
		const parent = child.parentNode;
		if (!parent) return;
		parent.childNodes.splice(parent.childNodes.indexOf(child), 1);
		child.parentNode = null;
	}
	private ownChild(node: FakeNode, role: string): void {
		if (node.parentNode !== this) throw new Error(`NotFoundError: the ${role} node is not a child of ${this.nodeName}`);
	}
	appendChild(child: FakeNode): FakeNode {
		FakeNode.detach(child);
		child.parentNode = this;
		this.childNodes.push(child);
		return child;
	}
	insertBefore(child: FakeNode, before: FakeNode | null): FakeNode {
		if (before === null) return this.appendChild(child);
		this.ownChild(before, "reference");
		if (child === before) return child;
		FakeNode.detach(child);
		child.parentNode = this;
		this.childNodes.splice(this.childNodes.indexOf(before), 0, child);
		return child;
	}
	removeChild(child: FakeNode): FakeNode {
		this.ownChild(child, "removed");
		FakeNode.detach(child);
		return child;
	}
	addEventListener(): void {}
	removeEventListener(): void {}
}

class FakeElement extends FakeNode {
	readonly attributes = new Map<string, string>();
	readonly style: Record<string, string> = {};
	readonly namespaceURI = "http://www.w3.org/1999/xhtml";
	constructor(tag: string) {
		super(1, tag.toUpperCase());
	}
	get tagName(): string {
		return this.nodeName;
	}
	setAttribute(name: string, value: unknown): void {
		this.attributes.set(name, String(value));
	}
	removeAttribute(name: string): void {
		this.attributes.delete(name);
	}
	getAttribute(name: string): string | null {
		return this.attributes.get(name) ?? null;
	}
	set textContent(text: string) {
		for (const child of [...this.childNodes]) this.removeChild(child);
		if (text !== "") this.appendChild(fakeDocument.createTextNode(text));
	}
}

class FakeDocument extends FakeNode {
	activeElement: FakeElement | null = null;
	readonly documentElement: FakeElement;
	constructor() {
		super(9, "#document");
		this.documentElement = this.createElement("html");
	}
	createElement(tag: string): FakeElement {
		const element = new FakeElement(tag);
		element.ownerDocument = this;
		return element;
	}
	createTextNode(text: string): FakeNode {
		const node = new FakeNode(3, "#text");
		node.nodeValue = text;
		node.ownerDocument = this;
		return node;
	}
}

const fakeDocument = new FakeDocument();
{
	const scope = globalThis as Record<string, unknown>;
	scope.window = globalThis;
	scope.document = fakeDocument;
	scope.HTMLIFrameElement = class {};
	scope.IS_REACT_ACT_ENVIRONMENT = true;

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
