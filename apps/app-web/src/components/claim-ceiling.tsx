/**
 * #494 P0a: the strategy claim ceiling, always on screen wherever strategy
 * outputs render (rankings, strategy decisions). The deployed strategy runs
 * carry claim_ceiling='preview' — screened candidates with NO realized-PnL
 * evidence (vision.md: the success state "does not by itself prove …
 * positive alpha"). Deliberately not dismissable and not conditional on
 * data state: if the page shows strategy output, the ceiling shows.
 */
export function ClaimCeilingBanner() {
  return (
    <p
      role="note"
      className="mt-4 rounded-lg border border-amber-400/40 bg-amber-400/10 px-4 py-3 text-sm text-amber-300"
    >
      <span className="font-semibold uppercase tracking-wide">Preview</span> — these are screened
      candidates from a versioned strategy definition with no realized-PnL evidence
      (claim ceiling: <code>preview</code>). Not investment advice.
    </p>
  );
}
