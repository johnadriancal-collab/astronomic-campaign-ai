"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { cn } from "@/lib/utils";

type Health = "checking" | "ok" | "down";

const TOP_LEVEL_AREAS = [
  { href: "/manager", label: "Campaign Manager" },
  { href: "/crm", label: "CRM" },
];

function isAreaActive(pathname: string, href: string) {
  return pathname === href || pathname.startsWith(`${href}/`);
}

export function SiteHeader() {
  const pathname = usePathname();
  const [health, setHealth] = useState<Health>("checking");

  useEffect(() => {
    let cancelled = false;
    fetch("/backend/health")
      .then((res) => (res.ok ? res.json() : Promise.reject()))
      .then((data) => {
        if (!cancelled) setHealth(data?.status === "ok" ? "ok" : "down");
      })
      .catch(() => {
        if (!cancelled) setHealth("down");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <header className="sticky top-0 z-40 border-b border-border/60 bg-background/80 backdrop-blur-md">
      <div className="mx-auto flex h-14 max-w-5xl items-center justify-between px-4 sm:px-6">
        <div className="flex items-center gap-6">
          <Link href="/" className="flex items-center gap-2 whitespace-nowrap font-medium tracking-tight">
            <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-primary text-primary-foreground text-xs font-semibold">
              A
            </span>
            <span className="text-sm sm:text-base">
              <span className="sm:hidden">Campaign AI</span>
              <span className="hidden sm:inline">Astronomic Campaign AI</span>
            </span>
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
        <div className="flex items-center gap-2 whitespace-nowrap text-xs text-muted-foreground">
          <span
            className={
              "h-1.5 w-1.5 shrink-0 rounded-full transition-colors " +
              (health === "ok"
                ? "bg-emerald-400"
                : health === "down"
                ? "bg-red-400"
                : "bg-muted-foreground/40 animate-pulse")
            }
          />
          <span className="hidden sm:inline">
            {health === "ok" ? "Backend connected" : health === "down" ? "Backend unreachable" : "Connecting…"}
          </span>
        </div>
      </div>
    </header>
  );
}
