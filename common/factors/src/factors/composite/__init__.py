"""Composite factors — consume other factors' already-materialized mart outputs.

Still factors, not app-layer logic (init.md Section 1, rule 2).
A composite factor's confidence is the min() of everything it consumes.
"""
