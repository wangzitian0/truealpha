/**
 * #575 scope 2: the served run's age is a number and a verdict, on the invariant's threshold.
 * Run standalone: `bun run tests/served-run-age.test.ts`.
 */
import { STALE_AFTER_HOURS, describeServedRunAge } from "../src/app/research/served-run-age";

function assert(condition: unknown, message: string): asserts condition {
	if (!condition) throw new Error(message);
}

const now = new Date("2026-09-08T10:15:00Z");

{
	const fresh = describeServedRunAge("2026-09-07T22:15:00Z", now);
	assert(fresh !== null && fresh.hours === 12 && fresh.label === "12 h ago" && !fresh.stale, "last night's tick is fresh");
}
{
	// exactly the threshold is still fresh; one hour past it is stale — the same edge the
	// invariant's `> interval '36 hours'` draws
	const edge = describeServedRunAge(new Date(now.getTime() - STALE_AFTER_HOURS * 3_600_000).toISOString(), now);
	assert(edge !== null && edge.hours === 36 && !edge.stale, "36 h is the last fresh hour");
	const past = describeServedRunAge(new Date(now.getTime() - (STALE_AFTER_HOURS + 1) * 3_600_000).toISOString(), now);
	assert(past !== null && past.stale && past.label === "1 d 13 h ago", "37 h reads as a day and 13 hours, stale");
}
{
	const old = describeServedRunAge("2026-08-14T09:24:00Z", now); // the 2026-08-17 shape, 25 days later
	assert(old !== null && old.stale && old.label.endsWith(" ago") && old.hours === 600, "a three-week-old head is stale and counted in days");
}
{
	assert(describeServedRunAge("not a date", now) === null, "a malformed cutoff is absent, not 0 h ago");
	const future = describeServedRunAge("2026-09-09T00:00:00Z", now);
	assert(future !== null && future.hours === 0 && !future.stale, "a cutoff in the future clamps to 0 h");
}
assert(
	STALE_AFTER_HOURS === 36,
	"this side of the duplicated 36 h threshold (the other is tools/output_invariants.py pointer-has-advanced-recently) — change both or neither",
);
console.log("#575 served-run age passed");
