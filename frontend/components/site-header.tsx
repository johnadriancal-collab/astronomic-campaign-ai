"use client";

import Image from "next/image";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { cn } from "@/lib/utils";

const TOP_LEVEL_AREAS = [
  { href: "/", label: "AI Campaign Creator" },
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
    <header className="sticky top-0 z-40 border-b border-border/60 bg-background/80 backdrop-blur-md">
      <div className="mx-auto flex h-14 max-w-5xl items-center gap-6 px-4 sm:px-6">
        <Link href="/" className="flex shrink-0 items-center">
          <Image
            src="/astronomic-logo.png"
            alt="Astronomic"
            width={761}
            height={140}
            priority
            className="h-6 w-auto brightness-0 invert"
          />
        </Link>
        <nav className="hidden items-center gap-1 sm:flex">
          {TOP_LEVEL_AREAS.map((area) => {
            const active = isAreaActive(pathname, area.href);
            return (
              <Link
                key={area.href}
                href={area.href}
                className={cn(
                  "rounded-md px-2.5 py-1 text-sm transition-colors",
                  active ? "bg-secondary font-medium text-foreground" : "text-muted-foreground hover:text-foreground"
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
