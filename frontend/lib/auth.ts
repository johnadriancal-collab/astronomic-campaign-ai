// Pure helpers for the Hub login flow -- kept separate from page
// components so they're unit-testable without rendering React, same split
// as lib/mailboxes.ts and lib/timezones.ts.

// Prevents an open-redirect via the `?next=` query param: only a genuine
// same-app relative path is ever honored. `//evil.com` is browser-
// equivalent to `https://evil.com` (protocol-relative), and an absolute
// URL is rejected outright -- anything that doesn't look like a plain
// single-leading-slash path falls back to "/".
export function sanitizeNextPath(next: string | null | undefined): string {
  if (!next) return "/";
  if (!next.startsWith("/") || next.startsWith("//")) return "/";
  return next;
}

// Which request paths proxy.ts lets through WITHOUT a session cookie.
// Pure and framework-free on purpose (proxy.ts's NextRequest/NextResponse
// types only exist inside the Next.js runtime, not plain `node --test`) so
// this exact routing decision is directly unit-testable.
//
// "/backend/*" is next.config.ts's rewrite proxy to the real API -- these
// are fetch() calls from client-side JS, not page navigations, and they
// must reach the real backend (whose own session_auth_middleware.py is the
// actual enforcement point) rather than being redirected to an HTML login
// page. Next.js's Proxy step runs BEFORE next.config.ts's rewrites, so
// without this exclusion every "/backend/*" fetch while logged out --
// including the login form's own POST /backend/auth/login -- would be
// silently turned into a 200 response containing the /login page's HTML
// instead of ever reaching the backend at all.
//
// "/about", "/privacy", "/terms" (Google OAuth Branding prerequisite,
// 2026-09) are the Hub's only public informational/legal pages -- they
// carry no session/campaign/contact data of any kind (see app/about,
// app/privacy, app/terms), so excluding them here is safe: it is the
// SAME exclusion class as /login, not a weakening of it. No other route
// is added to this list -- every actual product surface (the root Astro
// AI page included) stays behind the session check.
const PUBLIC_PAGE_PATHS = ["/login", "/about", "/privacy", "/terms"];

export function isPublicProxyPath(pathname: string): boolean {
  if (pathname.startsWith("/backend/")) return true;
  return PUBLIC_PAGE_PATHS.some((p) => pathname === p || pathname.startsWith(`${p}/`));
}
