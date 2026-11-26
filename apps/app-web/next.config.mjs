/** @type {import('next').NextConfig} */
const nextConfig = {
  // Middleware proxies /api/v1/* to llm-service; app-web holds zero Postgres drivers or direct DB access (#1716).
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
