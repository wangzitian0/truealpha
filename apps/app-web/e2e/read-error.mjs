/**
 * #1707: a page catches a failed API read and renders `ReadStateNotice` with kind `error`, so the
 * HTTP status stays 200 and a route walk saw a healthy page while `/api/v1/entities/holdings` returned
 * 500. The notice carries `data-read-state`; a visible error notice fails the route.
 */
export const READ_ERROR_SELECTOR = '[data-read-state="error"]';

/** Return one problem per error notice text that the route renders. */
export function readErrorProblems(path, texts) {
  return texts.map((text) => `read error on ${path}: ${text.trim().replace(/\s+/g, " ").slice(0, 200)}`);
}

/** Add a problem for each visible read-error notice on the current page. */
export async function assertNoReadError(page, path, problems) {
  const texts = await page.locator(READ_ERROR_SELECTOR).allInnerTexts();
  problems.push(...readErrorProblems(path, texts));
}
