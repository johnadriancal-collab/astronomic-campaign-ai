"use client";

import { useEffect, useState } from "react";

type Health = "checking" | "ok" | "down";

export function BackendStatus() {
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
    <div className="fixed bottom-3 left-3 z-40 flex items-center gap-1.5 whitespace-nowrap rounded-full border border-border/40 bg-background/80 px-2 py-1 text-[11px] text-muted-foreground backdrop-blur-md">
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
      <span>{health === "ok" ? "Backend connected" : health === "down" ? "Backend unreachable" : "Connecting…"}</span>
    </div>
  );
}
