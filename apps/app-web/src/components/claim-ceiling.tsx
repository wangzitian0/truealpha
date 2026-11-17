/** Unconditional strategy claim ceiling banner (#494 P0a). */
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
