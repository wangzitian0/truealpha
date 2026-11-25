export interface ClaimCeilingProps {
  claimCeiling?: string | null;
  reviewState?: string | null;
  definitionVersion?: string | null;
}

export function ClaimCeilingBanner({ claimCeiling, reviewState, definitionVersion }: ClaimCeilingProps) {
  const ceiling = (claimCeiling || "preview").trim();
  const details = [definitionVersion ? `definition ${definitionVersion}` : null, reviewState ? `review: ${reviewState}` : null]
    .filter(Boolean)
    .join(" · ");

  return (
    <p
      role="note"
      data-claim-ceiling={ceiling}
      data-review-state={reviewState ?? "unknown"}
      className="mt-4 rounded-lg border border-amber-400/40 bg-amber-400/10 px-4 py-3 text-sm text-amber-300"
    >
      <span className="font-semibold uppercase tracking-wide">{ceiling}</span> — these are screened
      candidates from a versioned strategy definition with no realized-PnL evidence (claim ceiling:{" "}
      <code>{ceiling}</code>
      {details ? ` · ${details}` : ""}). Not investment advice.
    </p>
  );
}
