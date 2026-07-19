"""Packaged runtime data for the live TOPT pipeline (#27).

`corpus.v1.json` is the frozen capture-control universe corpus — versioned scope
configuration (init.md rule 13). It ships INSIDE the wheel because the deployed
image contains only site-packages, not the repo tree; the tests/fixtures copy
stays canonical for tests and a conformance test pins both copies byte-identical.
"""
