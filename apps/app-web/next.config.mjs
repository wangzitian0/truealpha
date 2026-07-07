/** @type {import('next').NextConfig} */
const nextConfig = {
  // No API proxy: the app reads the mart schema in Postgres directly from its own
  // server-side code (init.md Section 1, rule 5). FastAPI is LLM orchestration only.
};

export default nextConfig;
