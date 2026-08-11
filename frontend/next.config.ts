import type { NextConfig } from "next";

const BACKEND_ORIGIN = process.env.BACKEND_ORIGIN ?? "http://localhost:8000";

const nextConfig: NextConfig = {
  async rewrites() {
    return [
      {
        source: "/backend/:path*",
        destination: `${BACKEND_ORIGIN}/:path*`,
      },
    ];
  },
  async redirects() {
    return [
      // Astro Search moved under the CRM layout so it keeps the sidebar
      // visible like every other CRM section (see app/crm/astro/page.tsx) --
      // this keeps old /astro links/bookmarks working without a second copy
      // of the page.
      {
        source: "/astro",
        destination: "/crm/astro",
        permanent: false,
      },
    ];
  },
  experimental: {
    // Next's rewrite proxy kills the upstream connection after 30s by
    // default. The ranking call in /campaign/search regularly runs
    // 20-35s, so it was getting cut off mid-request (ECONNRESET) even
    // though the backend was processing it correctly the whole time.
    proxyTimeout: 120000,
  },
};

export default nextConfig;
