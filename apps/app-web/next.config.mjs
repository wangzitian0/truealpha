/** @type {import('next').NextConfig} */
const nextConfig = {
  // No API proxy: the app reads the mart schema in Postgres directly from its own
  // server-side code (init.md Section 1, rule 5). FastAPI is LLM orchestration only.
  // Standalone output: the Docker runtime image runs `node server.js` without node_modules.
  output: "standalone",
  // #493 froze the URL tree (routes.frozen.json): strategy decisions moved into the
  // research world, the quality diagnostic moved into the operate world. Old URLs
  // permanent-redirect so no bookmark or external link dies.
  async redirects() {
    return [
      { source: "/admin/strategy-runs", destination: "/research/strategy", permanent: true },
      { source: "/research/quality", destination: "/admin/quality", permanent: true },
    ];
  },
};

export default nextConfig;
