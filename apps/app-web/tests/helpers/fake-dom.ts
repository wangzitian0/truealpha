/**
 * A small in-memory DOM for tests that mount a client component.
 *
 * It has only what React's client renderer touches for a few elements. The app
 * has no DOM library, and these checks do not justify adding one to the
 * lockfile. `tests/sign-out-button.test.ts` (#811) and
 * `tests/walk-tree-hydration.test.ts` (#1412) use it.
 *
 * Call `installFakeDom()` before anything imports `react-dom/client`: that
 * module reads the globals when it loads.
 */

export class FakeNode {
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

export class FakeElement extends FakeNode {
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
		if (text === "") return;
		if (!this.ownerDocument) throw new Error(`the fake ${this.nodeName} has no owner document`);
		this.appendChild(this.ownerDocument.createTextNode(text));
	}
}

export class FakeDocument extends FakeNode {
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

/** Makes a new fake document the global `document` and returns it. */
export function installFakeDom(): FakeDocument {
	const fakeDocument = new FakeDocument();
	const scope = globalThis as Record<string, unknown>;
	scope.window = globalThis;
	scope.document = fakeDocument;
	scope.HTMLIFrameElement = class {};
	scope.IS_REACT_ACT_ENVIRONMENT = true;
	return fakeDocument;
}
