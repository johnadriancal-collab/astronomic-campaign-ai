import type { NextConfig } from "next";

const BACKEND_ORIGIN = process.env.BACKEND_ORIGIN ?? "http://localhost:8000";

const nextConfig: NextConfig = {
  // Defense in depth ONLY, alongside app/layout.tsx's `robots` metadata --
  // the real security boundary is the login itself (middleware.ts /
  // session_auth_middleware.py), not this header.
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [{ key: "X-Robots-Tag", value: "noindex, nofollow" }],
      },
    ];
  },
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
      // Campaign Manager Integration Phase: Astronomic Mail's UI moved out
      // of the CRM layout into Campaign Manager, which is now the single
      // front door for campaigns of either sending method (see
      // app/manager/campaigns/*, app/manager/settings/page.tsx). These old
      // pages were removed, not duplicated -- these redirects are the only
      // remaining trace of the old paths.
      {
        source: "/crm/mail/campaigns",
        destination: "/manager/campaigns",
        permanent: false,
      },
      {
        source: "/crm/mail/campaigns/:id",
        destination: "/manager/campaigns/mail/:id",
        permanent: false,
      },
      {
        source: "/crm/mail/mailboxes",
        destination: "/manager/emails",
        permanent: false,
      },
      // Emails Inbox Management Phase: the old "Sequences / Emails" nav
      // item/placeholder is fully replaced by the real Emails page (see
      // app/manager/emails/page.tsx) -- there is no per-sequence detail to
      // send an old /manager/sequences/:id link to, so both collapse to the
      // same canonical destination rather than keeping a second, dead page.
      {
        source: "/manager/sequences",
        destination: "/manager/emails",
        permanent: false,
      },
      {
        source: "/manager/sequences/:id",
        destination: "/manager/emails",
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
