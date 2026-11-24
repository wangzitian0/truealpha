/**
 * #1849: `findDetailLink` returns as soon as a list page shows a detail link, an empty-list notice or
 * a read-error notice, and waits the full timeout only when the page shows none of them.
 *
 * Before #1849 the walk waited for the link only. On a page with an empty list that wait always ran
 * to its 15 s timeout: 30 s of each staging walk, 60 s of each CI walk.
 *
 * This drives `findDetailLink` of `e2e/detail-link.mjs` against a fake page. Each fake element
 * appears at a given time. The fake parses each selector against the tag and attributes of its
 * elements and does not import the selector constants, so a lookup that waits for another notice,
 * or for the link only, fails here. `tests/list-empty-marker.test.ts` proves the other half: the
 * list pages render the notices that these selectors read.
 *
 * Run standalone: `bun run tests/detail-link.test.ts`.
 */

import { findDetailLink } from "../e2e/detail-link.mjs";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const POLL_MS = 5;
const TIMEOUT_MS = 1000;
// A lookup that returns on a present element takes a few polls. The bound is far below TIMEOUT_MS,
// so a lookup that waits for the timeout fails it, and timer jitter cannot fail a correct lookup.
const PROMPT_MS = 400;

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

type FakeElement = { tag: string; attributes: Record<string, string>; atMs: number };

/** `tag[name^="v"]`, `tag[name="v"]`, `[name="v"]`. Any other selector fails the test loudly. */
function matches(selector: string, element: FakeElement): boolean {
  const parsed = selector.match(/^([a-z]+)?\[([\w-]+)(\^?=)"([^"]*)"\]$/);
  assert(parsed !== null, `the fake page cannot read selector ${selector}`);
  const [, tag, name, operator, expected] = parsed;
  if (tag !== undefined && tag !== element.tag) return false;
  const actual = element.attributes[name];
  if (actual === undefined) return false;
  return operator === "^=" ? actual.startsWith(expected) : actual === expected;
}

class FakePage {
  private readonly startedAt = Date.now();
  constructor(private readonly elements: FakeElement[]) {}

  present(): FakeElement[] {
    const now = Date.now() - this.startedAt;
    return this.elements.filter((element) => element.atMs <= now);
  }

  locator(selector: string): FakeLocator {
    return new FakeLocator(this, [selector]);
  }
}

class FakeLocator {
  constructor(
    private readonly page: FakePage,
    private readonly selectors: string[],
  ) {}

  private matching(): FakeElement[] {
    return this.page.present().filter((element) => this.selectors.some((selector) => matches(selector, element)));
  }

  or(other: FakeLocator): FakeLocator {
    return new FakeLocator(this.page, [...this.selectors, ...other.selectors]);
  }

  first(): FakeLocator {
    return this;
  }

  async count(): Promise<number> {
    return this.matching().length;
  }

  async getAttribute(name: string): Promise<string | null> {
    const [element] = this.matching();
    assert(element !== undefined, `getAttribute(${name}) on a locator with no element: ${this.selectors.join(" | ")}`);
    return element.attributes[name] ?? null;
  }

  async waitFor({ state, timeout }: { state: string; timeout: number }): Promise<void> {
    assert(state === "attached", `findDetailLink waits for state ${state}, not attached`);
    const deadline = Date.now() + timeout;
    while (this.matching().length === 0) {
      if (Date.now() >= deadline) throw new Error(`Timeout ${timeout}ms exceeded waiting for ${this.selectors.join(" | ")}`);
      await sleep(POLL_MS);
    }
  }
}

const link = (href: string, atMs = 0): FakeElement => ({ tag: "a", attributes: { href }, atMs });
const notice = (kind: string, atMs = 0): FakeElement => ({ tag: "p", attributes: { "data-read-state": kind }, atMs });

const scenarios: Array<{ name: string; elements: FakeElement[]; expected: object; prompt: boolean }> = [
  { name: "an empty list", elements: [notice("empty")], expected: { kind: "empty" }, prompt: true },
  { name: "a link that renders after 30 ms", elements: [link("/research/library/doc%3A1", 30)], expected: { kind: "link", href: "/research/library/doc%3A1" }, prompt: true },
  { name: "a read error", elements: [notice("error")], expected: { kind: "read-error" }, prompt: true },
  { name: "a link and an empty notice", elements: [notice("empty"), link("/research/library/doc%3A2")], expected: { kind: "link", href: "/research/library/doc%3A2" }, prompt: true },
  { name: "a link of another list and an empty notice", elements: [link("/research/backtest/run%3A1"), notice("empty")], expected: { kind: "empty" }, prompt: true },
  { name: "an unavailable notice only", elements: [notice("unavailable")], expected: { kind: "none" }, prompt: false },
  { name: "nothing", elements: [], expected: { kind: "none" }, prompt: false },
];

for (const scenario of scenarios) {
  const page = new FakePage(scenario.elements);
  const started = Date.now();
  const found = await findDetailLink(page, "/research/library/", { timeoutMs: TIMEOUT_MS });
  const elapsed = Date.now() - started;
  assert(
    JSON.stringify(found) === JSON.stringify(scenario.expected),
    `${scenario.name}: expected ${JSON.stringify(scenario.expected)}, got ${JSON.stringify(found)}`,
  );
  if (scenario.prompt) {
    assert(elapsed < PROMPT_MS, `${scenario.name}: the lookup took ${elapsed} ms; it must return before ${PROMPT_MS} ms, not wait for the ${TIMEOUT_MS} ms timeout`);
  } else {
    assert(elapsed >= TIMEOUT_MS, `${scenario.name}: the lookup returned after ${elapsed} ms, before the ${TIMEOUT_MS} ms timeout`);
  }
}

console.log(`ok   detail-link: ${scenarios.length} list-page states, each answered without a needless wait`);
