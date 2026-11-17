export const STALE_HOURS = 36;

export interface ServedRunAge {
	hours: number;
	label: string;
	stale: boolean;
}

export function describeServedRunAge(
	cutoffIso: string,
	now: Date = new Date(),
	staleHours: number = STALE_HOURS,
): ServedRunAge | null {
	const cutoff = new Date(cutoffIso);
	if (Number.isNaN(cutoff.getTime())) return null;
	const hours = Math.max(0, Math.floor((now.getTime() - cutoff.getTime()) / 3_600_000));
	const days = Math.floor(hours / 24);
	const label = days >= 1 ? `${days} d ${hours - days * 24} h ago` : `${hours} h ago`;
	return { hours, label, stale: hours > staleHours };
}
