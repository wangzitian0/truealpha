# Gate 0 Candidate Evidence

`manifest-v4.json` binds the complete Gate 0 issue set in dependency order. It is a
review candidate, not accepted Gate evidence. The default candidate check proves that
the files, hashes, scope, dependency direction, and explicit blockers are internally
consistent. The acceptance check additionally requires every issue artifact and every
external attestation to be accepted; it must fail while any approval or evidence is
missing.

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
