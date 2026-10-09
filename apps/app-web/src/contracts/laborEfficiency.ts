/**
 * #1176: the labor-efficiency metric names, and the labels a page shows next to a number.
 *
 * The strategy writes the name it used for each decision (mart.strategy_decisions.labor_efficiency_metric).
 * The published mart column `gppe` is the uniform metric for every class. This module only formats
 * labels. It never derives a name from an issuer class: the name comes from the decision row.
 */

/** The uniform total-assets charge. The published mart column `gppe` carries it for every class. */
export const PUBLISHED_GPPE_METRIC = "gppe_uniform_charge_v0";

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

/**
 * Every present number with its metric name. The published value and the strategy value collapse
 * into one entry only when they name the same metric and carry the same number.
 */
export function laborEfficiencyLabels(input: LaborEfficiencyInput): LaborEfficiencyLabel[] {
	const labels: LaborEfficiencyLabel[] = [];
	if (input.publishedGppe !== null) {
		labels.push({ metric: PUBLISHED_GPPE_METRIC, value: input.publishedGppe });
	}
	if (input.strategyValue !== null) {
		const metric = input.strategyMetric ?? UNRECORDED_METRIC;
		const duplicate = labels.some(
			(label) => label.metric === metric && label.value === input.strategyValue,
		);
		if (!duplicate) labels.push({ metric, value: input.strategyValue });
	}
	return labels;
}
