-- 0044: pin raw.canonical_sha256's search_path so restores are lossless (#650).
--
-- The first restore drill (2026-08-20) proved the backup was silently
-- unrestorable-in-full: pg_dump replays with an EMPTY search_path, and
-- canonical_sha256 (0023) calls pgcrypto's digest() unqualified, so every
-- COPY into a table whose CHECK invokes it failed — mart.topt_core_invocations
-- and mart.topt_gppe_invocations restored as 0 rows (43 live) with only two
-- ERROR lines in a 12MB replay to say so. Pinning the function's search_path
-- makes it caller-independent; the drill re-run must restore row-exact.
alter function raw.canonical_sha256(jsonb) set search_path = public, pg_catalog;
