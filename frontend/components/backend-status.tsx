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
    <div className="fixed bottom-3 left-3 z-40 flex items-center gap-1.5 whitespace-nowrap text-[11px] text-muted-foreground/70">
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
      <span>{health === "ok" ? "Connected" : health === "down" ? "Unreachable" : "Connecting…"}</span>
    </div>
  );
}
