"use client";

import { useEffect, useState } from "react";
import { usePathname } from "next/navigation";

type Health = "checking" | "ok" | "down";

function useBackendHealth(): Health {
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

  return health;
}

function StatusDot({ health }: { health: Health }) {
  return (
    <span
      className={
        "h-1.5 w-1.5 shrink-0 rounded-full transition-colors " +
        (health === "ok"
          ? "bg-emerald-600"
          : health === "down"
          ? "bg-destructive"
          : "bg-muted-foreground/40 animate-pulse")
      }
    />
  );
}

function statusLabel(health: Health) {
  return health === "ok" ? "Connected" : health === "down" ? "Unreachable" : "Connecting…";
}

// Viewport-fixed indicator shown on every page EXCEPT inside /crm, which
// renders its own BackendStatusRow inline at the bottom of the CRM sidebar
// instead. Both being pinned to the same bottom-left corner would overlap
// once the sidebar's own bottom content (the Settings link) reaches there.
export function BackendStatus() {
  const pathname = usePathname();
  const health = useBackendHealth();

  if (pathname?.startsWith("/crm")) return null;

  return (
    <div className="fixed bottom-3 left-3 z-40 flex items-center gap-1.5 whitespace-nowrap text-[11px] text-muted-foreground/70">
      <StatusDot health={health} />
      <span>{statusLabel(health)}</span>
    </div>
  );
}

// Inline variant for the CRM sidebar's bottom section -- a normal flow row,
// not fixed, so it never competes for screen space with anything else.
export function BackendStatusRow() {
  const health = useBackendHealth();

  return (
    <div className="flex items-center gap-1.5 px-3 py-1 text-[11px] text-muted-foreground/70">
      <StatusDot health={health} />
      <span>{statusLabel(health)}</span>
    </div>
  );
}
