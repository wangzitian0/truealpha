/**
 * #44 / #1744 / R5.E9: 1080x1440 Xiaohongshu card export & Playwright geometry/overflow check.
 */

export const EXPECTED_CARD_WIDTH = 1080;
export const EXPECTED_CARD_HEIGHT = 1440;

/**
 * Validate DOM layout metrics measured inside a 1080x1440 Playwright page.
 *
 * @param {{
 *   hasCanvas: boolean,
 *   width: number,
 *   height: number,
 *   clientWidth: number,
 *   clientHeight: number,
 *   scrollWidth: number,
 *   scrollHeight: number,
 *   canvasBottom?: number | null,
 *   topBottom?: number | null,
 *   footerTop?: number | null,
 *   footerBottom?: number | null,
 * }} metrics
 * @returns {string[]}
 */
export function evaluateCardGeometry(metrics) {
  if (!metrics || !metrics.hasCanvas) {
    return ["[card-geometry] missing .card-canvas root element"];
  }
  const problems = [];
  if (Math.round(metrics.width) !== EXPECTED_CARD_WIDTH || Math.round(metrics.height) !== EXPECTED_CARD_HEIGHT) {
    problems.push(
      `[card-geometry] expected ${EXPECTED_CARD_WIDTH}x${EXPECTED_CARD_HEIGHT} canvas, got ${metrics.width}x${metrics.height}`,
    );
  }
  if (metrics.scrollWidth > metrics.clientWidth) {
    problems.push(`[card-geometry] horizontal overflow (${metrics.scrollWidth} > ${metrics.clientWidth})`);
  }
  if (metrics.scrollHeight > metrics.clientHeight) {
    problems.push(`[card-geometry] vertical overflow (${metrics.scrollHeight} > ${metrics.clientHeight})`);
  }
  if (metrics.topBottom != null && metrics.footerTop != null && metrics.topBottom > metrics.footerTop + 1) {
    problems.push(`[card-geometry] body overlaps footer (${metrics.topBottom} > ${metrics.footerTop})`);
  }
  if (metrics.footerBottom != null && metrics.canvasBottom != null && metrics.footerBottom > metrics.canvasBottom + 1) {
    problems.push(`[card-geometry] footer extends past canvas bottom (${metrics.footerBottom} > ${metrics.canvasBottom})`);
  }
  return problems;
}

/**
 * Measure `.card-canvas` geometry & overflow inside a Playwright Page after `setContent`.
 *
 * @param {import("playwright").Page} page
 * @returns {Promise<string[]>}
 */
export async function measureCardPage(page) {
  const metrics = await page.evaluate(() => {
    const canvas = document.querySelector(".card-canvas");
    if (!canvas) {
      return {
        hasCanvas: false,
        width: 0,
        height: 0,
        clientWidth: 0,
        clientHeight: 0,
        scrollWidth: 0,
        scrollHeight: 0,
      };
    }
    const rect = canvas.getBoundingClientRect();
    const topBody = canvas.firstElementChild;
    const footer = canvas.querySelector(".card-footer");
    return {
      hasCanvas: true,
      width: rect.width,
      height: rect.height,
      clientWidth: canvas.clientWidth,
      clientHeight: canvas.clientHeight,
      scrollWidth: canvas.scrollWidth,
      scrollHeight: canvas.scrollHeight,
      canvasBottom: rect.bottom,
      topBottom: topBody ? topBody.getBoundingClientRect().bottom : null,
      footerTop: footer ? footer.getBoundingClientRect().top : null,
      footerBottom: footer ? footer.getBoundingClientRect().bottom : null,
    };
  });
  return evaluateCardGeometry(metrics);
}

/**
 * Export a 1080x1440 HTML card from `/api/v1/library/cards/export` (`persist: false`)
 * and verify its rendered DOM geometry in Playwright Chromium.
 *
 * @param {{ browser: import("playwright").Browser, base: string, email: string, password: string }} options
 * @returns {Promise<string[]>}
 */
export async function walkCardGeometry({ browser, base, email, password }) {
  const context = await browser.newContext({
    viewport: { width: EXPECTED_CARD_WIDTH, height: EXPECTED_CARD_HEIGHT },
  });
  try {
    const login = await context.request.post(`${base}/api/v1/auth/login`, {
      data: { email, password },
    });
    if (login.status() !== 200) {
      return [`[card-geometry] login answered HTTP ${login.status()}`];
    }
    const exported = await context.request.post(`${base}/api/v1/library/cards/export`, {
      data: {
        card_kind: "ranking",
        universe: "all",
        convention: "historical_cagr",
        format: "html",
        persist: false,
      },
    });
    if (exported.status() !== 200) {
      return [`[card-geometry] POST /api/v1/library/cards/export answered HTTP ${exported.status()}: ${await exported.text()}`];
    }
    const payload = await exported.json();
    if (!payload?.rendered_html || typeof payload.rendered_html !== "string") {
      return ["[card-geometry] POST /api/v1/library/cards/export returned empty rendered_html"];
    }
    const page = await context.newPage();
    await page.setContent(payload.rendered_html, { waitUntil: "load" });
    return await measureCardPage(page);
  } finally {
    await context.close();
  }
}
