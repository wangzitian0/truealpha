/**
 * #1851: gross segments with an intersegment elimination as the residual count against their
 * own sum, so the classified share never reads above 100%. A positive residual stays a gap.
 */

import { classifiedShare } from "../src/client/strategy";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

// INTC's 10-K: four segments sum to 70,536M against 52,853M; the elimination of -17,683M is the residual.
const gross = classifiedShare({
  in_theme_revenue: "34745",
  out_of_theme_revenue: "35791",
  consolidated_revenue: "52853",
  partition_residual: "-17683",
});
assert(gross === "100.0%", `gross segments read ${gross}, not 100.0%`);

const gap = classifiedShare({
  in_theme_revenue: "40000",
  out_of_theme_revenue: "10000",
  consolidated_revenue: "60000",
  partition_residual: "10000",
});
assert(gap === "83.3%", `a positive residual is unclassified revenue; read ${gap}, not 83.3%`);

const unstated = classifiedShare({ in_theme_revenue: "30", out_of_theme_revenue: "20", consolidated_revenue: "100" });
assert(unstated === "50.0%", `a row without a residual reads ${unstated}, not 50.0%`);
