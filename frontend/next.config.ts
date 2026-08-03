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
  experimental: {
    // Next's rewrite proxy kills the upstream connection after 30s by
    // default. The ranking call in /campaign/search regularly runs
    // 20-35s, so it was getting cut off mid-request (ECONNRESET) even
    // though the backend was processing it correctly the whole time.
    proxyTimeout: 120000,
  },
};

export default nextConfig;
