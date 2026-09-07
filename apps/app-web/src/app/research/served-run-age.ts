/**
 * #575 scope 2: the staleness of what /research serves is rendered, not implied.
 *
 * The overview names the served run's cutoff; a reader still had to do the arithmetic
 * against the clock to know whether it is this morning's run or one from three days
 * ago (the 2026-08-17 shape: a 67-hour-old head behind a page that said "Data as of"
 * and nothing else). This turns the cutoff into an age and a verdict on the threshold
 * `tools/output_invariants.py`'s `pointer-has-advanced-recently` uses — one daily cycle
 * plus slack, 36 hours. The constant is duplicated here on purpose (the page cannot
 * import a Python tool); `tests/served-run-age.test.ts` pins this side's value so a
 * change to either has to be made in both.
 */

export const STALE_AFTER_HOURS = 36;

export interface ServedRunAge {
	/** Whole hours between the cutoff and `now`; never negative. */
	hours: number;
	/** "2 h ago", "31 h ago", "3 d 4 h ago" — the same number a person would say. */
	label: string;
	/** True past one scheduled cycle plus slack (36 h) — the same age at which the nightly
	 * invariant raises amber. */
	stale: boolean;
}

/** `null` when the cutoff cannot be read as an instant, so a malformed value is shown
 * as absent rather than as "0 h ago". */
export function describeServedRunAge(cutoffIso: string, now: Date = new Date()): ServedRunAge | null {
	const cutoff = new Date(cutoffIso);
	if (Number.isNaN(cutoff.getTime())) return null;
	const hours = Math.max(0, Math.floor((now.getTime() - cutoff.getTime()) / 3_600_000));
	const days = Math.floor(hours / 24);
	const label = days >= 1 ? `${days} d ${hours - days * 24} h ago` : `${hours} h ago`;
	return { hours, label, stale: hours > STALE_AFTER_HOURS };
}
