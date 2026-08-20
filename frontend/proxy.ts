import { NextRequest, NextResponse } from "next/server";
import { isPublicProxyPath } from "@/lib/auth";

// Astronomic Hub internal login -- UX convenience layer only. This proxy
// (formerly "middleware" -- see Next.js's middleware-to-proxy migration)
// does a cheap "is the session cookie present" check so a logged-out
// visitor gets redirected to /login immediately rather than seeing a page
// that then fails every data fetch -- it does NOT validate the cookie's
// contents. The REAL security boundary is the backend's
// session_auth_middleware.py, which validates every request independently
// and cannot be bypassed by anything this file does or doesn't do.
const SESSION_COOKIE_NAME = "astro_session";

export function proxy(request: NextRequest) {
  const { pathname, search } = request.nextUrl;

  if (isPublicProxyPath(pathname)) {
    return NextResponse.next();
  }

  const hasSessionCookie = request.cookies.has(SESSION_COOKIE_NAME);
  if (!hasSessionCookie) {
    const loginUrl = new URL("/login", request.url);
    loginUrl.searchParams.set("next", pathname + search);
    return NextResponse.redirect(loginUrl);
  }

  return NextResponse.next();
}

export const config = {
  // Everything except Next's own static/image assets and the favicon --
  // in particular this DOES cover "/", "/crm/*", and "/manager/*".
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
