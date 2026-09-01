"use client";

import Image from "next/image";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { LogOut } from "lucide-react";
import { Button } from "@/components/ui/button";
import { logout } from "@/lib/api";
import { isPublicProxyPath } from "@/lib/auth";
import { TOP_LEVEL_NAV_AREAS } from "@/lib/top-level-nav";
import { cn } from "@/lib/utils";

function isAreaActive(pathname: string, href: string) {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}

export function SiteHeader() {
  const pathname = usePathname();

  // Every public page (login, plus the OAuth-branding pages /about,
  // /privacy, /terms -- see lib/auth.ts's isPublicProxyPath, the SAME
  // predicate proxy.ts itself uses) never shows the authenticated
  // shell's nav/logout -- there is no session to log out of on any of
  // them, and showing links to protected product areas to a logged-out
  // visitor would be confusing, not useful.
  const isLoginPage = pathname === "/login";
  const isPublicPage = isPublicProxyPath(pathname);

  async function handleLogout() {
    try {
      await logout();
    } finally {
      window.location.href = "/login";
    }
  }

  return (
    <header className="sticky top-0 z-40 border-b border-border bg-background/96 backdrop-blur-md">
      <div className="flex h-16 items-center justify-between px-4 sm:px-6">
        <Link href="/" className="flex shrink-0 items-center">
          {/* Official Astronomic Hub lockup (icon + wordmark), used exactly
              as provided. The source PNG has generous transparent padding on
              every edge (visible ink is only ~30% of the canvas height and
              ~93% of its width), so instead of rendering the full padded
              canvas at logo scale -- which would either force a taller
              header or shrink the mark into illegibility -- this crops via
              CSS: the image renders large enough for the ink to hit the same
              ~20px-tall scale as the old logo, then an overflow-hidden
              window (sized and positioned from the ink's own pixel bounds)
              shows only that ink, keeping the header at its original h-16
              with the mark aligned to the sidebar's left edge below. The
              PNG file itself is untouched. */}
          <div className="relative h-5 w-[141.8px] overflow-hidden">
            <Image
              src="/astronomic-hub-logo.png"
              alt="Astronomic Hub"
              width={2688}
              height={1152}
              priority
              className="absolute -top-[21.6px] -left-[6.5px] h-[65.5px] w-[152.7px]"
            />
          </div>
        </Link>
        {!isPublicPage && (
          <div className="flex items-center gap-4">
            <nav className="hidden items-center gap-6 sm:flex">
              {TOP_LEVEL_NAV_AREAS.map((area) => {
                const active = isAreaActive(pathname, area.href);
                return (
                  <Link
                    key={area.href}
                    href={area.href}
                    className={cn(
                      "border-b-2 border-transparent py-1 text-sm font-medium transition-colors",
                      active ? "border-primary text-foreground" : "text-muted-foreground hover:text-foreground"
                    )}
                  >
                    {area.label}
                  </Link>
                );
              })}
            </nav>
            <Button variant="ghost" size="sm" className="gap-1.5 text-muted-foreground" onClick={handleLogout}>
              <LogOut className="h-3.5 w-3.5" />
              Log out
            </Button>
          </div>
        )}
        {isPublicPage && !isLoginPage && (
          <Link href="/login" className="text-sm font-medium text-muted-foreground hover:text-foreground">
            Sign in
          </Link>
        )}
      </div>
    </header>
  );
}
