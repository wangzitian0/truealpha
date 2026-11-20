/**
 * #44 / #1744 / R5.E9: Unit checks for `evaluateCardGeometry` ensuring zero-overflow
 * and exact 1080x1440 dimension enforcement fail on mutated geometry metrics.
 */

import { evaluateCardGeometry } from "../e2e/card-geometry.mjs";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const clean = {
  hasCanvas: true,
  width: 1080,
  height: 1440,
  clientWidth: 1080,
  clientHeight: 1440,
  scrollWidth: 1080,
  scrollHeight: 1440,
  canvasBottom: 1440,
  topBottom: 1200,
  footerTop: 1220,
  footerBottom: 1416,
};
assert(evaluateCardGeometry(clean).length === 0, "clean 1080x1440 card metrics must pass");

const missing = evaluateCardGeometry({ ...clean, hasCanvas: false });
assert(missing.some((p) => p.includes("missing .card-canvas")), "missing canvas must fail");

const badSize = evaluateCardGeometry({ ...clean, width: 800, height: 600 });
assert(badSize.some((p) => p.includes("1080x1440")), "wrong dimensions must fail");

const overflow = evaluateCardGeometry({ ...clean, scrollWidth: 1120, scrollHeight: 1580 });
assert(overflow.some((p) => p.includes("horizontal overflow")), "horizontal overflow must fail");
assert(overflow.some((p) => p.includes("vertical overflow")), "vertical overflow must fail");

const overlap = evaluateCardGeometry({ ...clean, topBottom: 1250, footerTop: 1220, footerBottom: 1455 });
assert(overlap.some((p) => p.includes("overlaps footer")), "body-footer overlap must fail");
assert(overlap.some((p) => p.includes("past canvas bottom")), "footer past canvas bottom must fail");

console.log("#44 / R5.E9 card-geometry pure evaluator checks passed");
