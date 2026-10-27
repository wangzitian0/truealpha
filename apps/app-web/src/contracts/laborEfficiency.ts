/**
 * #1176, #1518: the labor-efficiency metric names, and the labels a page shows next to a number.
 *
 * The strategy writes the name it used for each decision (mart.strategy_decisions.labor_efficiency_metric).
 * That name is the root of the tree that the strategy definition pins for the issuer class. Since
 * definition 1.1.0 the TOPT core evaluates the same pinned tree, so the published `gppe` column and the
 * strategy value are one metric for every class. A published row carries no metric name of its own.
 * This module only formats labels. It never derives a name from an issuer class and it keeps no name of
 * its own: a name comes from a row.
 */

import { formatRatio } from "@/client/format";

/** Shown when a strategy value has no recorded metric name (a row written before #1176). */
export const UNRECORDED_METRIC = "metric not recorded";

export interface LaborEfficiencyLabel {
	metric: string;
	value: string;
}

export interface LaborEfficiencyInput {
	publishedGppe: string | null;
	strategyValue: string | null;
	strategyMetric: string | null;
}

function sameMetricValue(a: string, b: string): boolean {
	if (a === b) return true;
	const ratioA = formatRatio(a);
	const ratioB = formatRatio(b);
	if (ratioA === null || ratioB === null) return false;
	return ratioA === ratioB;
}

/**
 * Every present number with its metric name. The published value carries the name of the strategy
 * value when both are present, the metric is recorded and the two numbers agree: the core and the
 * composite evaluate one pinned tree. A published value that disagrees with the strategy value, or
 * that has no recorded metric to agree with, is shown as `UNRECORDED_METRIC`. The page never asserts
 * a name that no row states.
 */
export function laborEfficiencyLabels(input: LaborEfficiencyInput): LaborEfficiencyLabel[] {
	const labels: LaborEfficiencyLabel[] = [];
	const strategyValue = input.strategyValue;
	const strategyMetric = input.strategyMetric;
	const agrees =
		input.publishedGppe !== null &&
		strategyValue !== null &&
		strategyMetric !== null &&
		sameMetricValue(input.publishedGppe, strategyValue);
	if (input.publishedGppe !== null) {
		labels.push({ metric: agrees && strategyMetric !== null ? strategyMetric : UNRECORDED_METRIC, value: input.publishedGppe });
	}
	if (strategyValue !== null && !agrees) {
		labels.push({ metric: strategyMetric ?? UNRECORDED_METRIC, value: strategyValue });
	}
	return labels;
}
