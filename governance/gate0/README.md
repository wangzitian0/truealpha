# Gate 0 Candidate Evidence

`manifest-v4.json` is the immutable historical candidate that first bound the complete
Gate 0 issue set in dependency order. `manifest-v5.json` is its additive successor: it
binds the exact v4 bytes plus the authoritative governed-access architecture, stable
contract export, and version-aware validation controls. v4 remains byte-for-byte
unchanged and blocked; v5 inherits every v4 issue artifact, external attestation,
merge policy, and blocking reason.

The default candidate check selects the highest checked-in manifest version. A caller
can validate a historical version explicitly with `--manifest`. Candidate validation
proves hashes, scope, dependency direction, successor bindings, and explicit blockers
are internally consistent. Acceptance additionally requires every issue artifact and
external attestation to be accepted; it must fail while any approval or evidence is
missing. Adding a successor never raises Gate 0 readiness or closes #56.

The chain is intentionally one way:

```text
#57 identity/time + #58 execution/lineage
                    -> #59 semantics/catalog/oracle
                    -> #60 source/rights/budget capability
                    -> #61 applicability/SLO/refresh/usage policy
                    -> #56 Gate 0 acceptance
```

Candidate files never contain protected holdout labels, synthetic approval identities,
or a manually set readiness boolean. Public development goldens are review inputs only.
Product-owner, independent-review, custody, rights, budget, and known-reference records
must bind the exact candidate hashes before an accepted artifact can replace a candidate.

The manifest records that #57 and #58 were already merged to `main` under the superseded
delivery protocol. The integration branch binds their exact immutable evidence into the
complete candidate, but it does not rewrite history or claim that the historical
no-partial-merge invariant was satisfied.
