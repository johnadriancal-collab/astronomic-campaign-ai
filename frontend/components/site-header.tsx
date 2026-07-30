"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

type Health = "checking" | "ok" | "down";

export function SiteHeader() {
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
        <Link href="/" className="flex items-center gap-2 whitespace-nowrap font-medium tracking-tight">
          <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-primary text-primary-foreground text-xs font-semibold">
            A
          </span>
          <span className="text-sm sm:text-base">
            <span className="sm:hidden">Campaign AI</span>
            <span className="hidden sm:inline">Astronomic Campaign AI</span>
          </span>
        </Link>
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
