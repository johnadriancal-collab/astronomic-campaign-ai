"use client";

import Image from "next/image";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { cn } from "@/lib/utils";

const TOP_LEVEL_AREAS = [
  { href: "/", label: "Astro AI" },
  { href: "/manager", label: "Campaign Manager" },
  { href: "/crm", label: "CRM" },
];

function isAreaActive(pathname: string, href: string) {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}

export function SiteHeader() {
  const pathname = usePathname();

  return (
    <header className="sticky top-0 z-40 border-b border-border bg-background/96 backdrop-blur-md">
      <div className="mx-auto flex h-16 max-w-5xl items-center gap-8 px-4 sm:px-6">
        <Link href="/" className="flex shrink-0 items-center">
          {/* Original dark-navy artwork, unfiltered -- on this light header
              it reads the same way the mark does on astronomic.com's own
              scrolled (white) header state. */}
          <Image src="/astronomic-logo.png" alt="Astronomic" width={761} height={140} priority className="h-5 w-auto" />
        </Link>
        <nav className="hidden items-center gap-6 sm:flex">
          {TOP_LEVEL_AREAS.map((area) => {
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
      </div>
    </header>
  );
}
