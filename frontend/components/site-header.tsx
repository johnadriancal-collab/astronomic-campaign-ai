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
