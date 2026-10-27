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
 * Every present number with its metric name. The decision row names the metric of its class. A published value
 * takes that name when the decision row names one and the two numbers do not disagree: the core and the
 * composite evaluate one pinned tree. They do not disagree when the strategy value is absent (an issuer
 * excluded after its tree was chosen) or when both numbers agree. A published value that disagrees with the
 * strategy value, or that no row names a metric for, is shown as `UNRECORDED_METRIC`. The page never asserts a
 * name that no row states.
 */
export function laborEfficiencyLabels(input: LaborEfficiencyInput): LaborEfficiencyLabel[] {
	const labels: LaborEfficiencyLabel[] = [];
	const { publishedGppe, strategyValue, strategyMetric } = input;
	const publishedMetric =
		publishedGppe !== null &&
		strategyMetric !== null &&
		(strategyValue === null || sameMetricValue(publishedGppe, strategyValue))
			? strategyMetric
			: null;
	if (publishedGppe !== null) {
		labels.push({ metric: publishedMetric ?? UNRECORDED_METRIC, value: publishedGppe });
	}
	// One entry when the published value already carries the name and the number of the strategy value.
	const collapsed = publishedMetric !== null && strategyValue !== null;
	if (strategyValue !== null && !collapsed) {
		labels.push({ metric: strategyMetric ?? UNRECORDED_METRIC, value: strategyValue });
	}
	return labels;
}
