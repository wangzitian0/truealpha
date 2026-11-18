/** @type {import('next').NextConfig} */
const nextConfig = {
  // No API proxy: the app reads the mart schema in Postgres directly via llm-service OpenAPI.
  // Standalone output: the Docker runtime image runs `node server.js` without node_modules.
  output: "standalone",
  async redirects() {
    return [
      { source: "/admin/strategy-runs", destination: "/research/rankings", permanent: true },
      { source: "/research/quality", destination: "/admin", permanent: true },
    ];
  },
};

export default nextConfig;
