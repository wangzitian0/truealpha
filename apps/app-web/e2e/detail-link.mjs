/**
 * #1849: the walk opens the first detail link of a list page, and an empty list costs no wait.
 *
 * The list pages of this app render on the server (`force-dynamic`). When `gotoStable` returns,
 * the page has hydrated, so a detail link is in the DOM or the page shows a state notice. The
 * walk waits for the first of three elements: a detail link, the empty-list notice, or the
 * read-error notice. Before #1849 it waited for the link only. An empty list then cost the full
 * 15 s timeout: 30 s of each staging walk (library, two roles) and 60 s of each CI walk
 * (backtest and library, two roles).
 *
 * `e2e/walk-tree.mjs` launches a browser when it is imported, so this module holds the lookup.
 * `tests/detail-link.test.ts` drives it against a fake page. `tests/list-empty-marker.test.ts`
 * proves the other half: the list pages render the notice that this selector reads.
 */

import { READ_ERROR_SELECTOR } from "./read-error.mjs";

/** `ReadStateNotice` with kind `empty` (src/components/read-state.tsx) carries this attribute. */
export const READ_EMPTY_SELECTOR = '[data-read-state="empty"]';

/** The longest wait for a list page to show a link or a notice. */
export const DETAIL_LINK_TIMEOUT_MS = 15_000;

/**
 * Find the first link whose href starts with `hrefPrefix`, or the state notice that the page shows
 * in its place.
 *
 * @param {any} page a Playwright page that `gotoStable` has opened
 * @param {string} hrefPrefix
 * @param {{ timeoutMs?: number }} [options]
 * @returns {Promise<{ kind: "link", href: string } | { kind: "empty" } | { kind: "read-error" } | { kind: "none" }>}
 *   A link wins over a notice. A read-error notice wins over an empty notice, so a failed read is
 *   never reported as an empty list. `none`: the page showed no link and no notice within the timeout.
 */
export async function findDetailLink(page, hrefPrefix, { timeoutMs = DETAIL_LINK_TIMEOUT_MS } = {}) {
  const link = page.locator(`a[href^="${hrefPrefix}"]`);
  const empty = page.locator(READ_EMPTY_SELECTOR);
  const readError = page.locator(READ_ERROR_SELECTOR);
  try {
    // `first()`: a Playwright `waitFor` is strict and fails when more than one element matches.
    await link.or(empty).or(readError).first().waitFor({ state: "attached", timeout: timeoutMs });
  } catch {
    return { kind: "none" };
  }
  if ((await link.count()) > 0) {
    const href = await link.first().getAttribute("href");
    if (href) return { kind: "link", href };
  }
  if ((await readError.count()) > 0) return { kind: "read-error" };
  if ((await empty.count()) > 0) return { kind: "empty" };
  return { kind: "none" };
}
