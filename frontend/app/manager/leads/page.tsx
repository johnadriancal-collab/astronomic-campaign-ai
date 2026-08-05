"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { AlertTriangle, Users } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { buttonVariants } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError, listLeads, type LeadListItem } from "@/lib/api";
import { cn } from "@/lib/utils";

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function leadName(lead: LeadListItem): string {
  return [lead.first_name, lead.last_name].filter(Boolean).join(" ") || "—";
}

export default function LeadsPage() {
  const [leads, setLeads] = useState<LeadListItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    (async () => {
      setError(null);
      try {
        const data = await listLeads();
        if (!cancelled) setLeads(data);
      } catch (err) {
        if (!cancelled) {
          setError(
            err instanceof ApiError
              ? `Couldn't load leads (${err.status}): ${err.message}`
              : "Couldn't reach the backend to load leads."
          );
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="mx-auto max-w-5xl px-6 py-10">
      <div className="mb-8">
        <h1 className="font-serif text-2xl font-medium tracking-tight sm:text-3xl">Leads</h1>
        <p className="mt-2 max-w-2xl text-sm text-muted-foreground">
          Prospects that became durable leads once a campaign built their Apollo contact.
        </p>
      </div>

      {error && (
        <Alert variant="destructive">
          <AlertTriangle />
          <AlertTitle>Couldn&apos;t load leads</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {!error && leads === null && (
        <div className="space-y-2">
          {Array.from({ length: 5 }).map((_, i) => (
            <Skeleton key={i} className="h-12 w-full rounded-lg" />
          ))}
        </div>
      )}

      {!error && leads !== null && leads.length === 0 && (
        <div className="flex flex-col items-center gap-4 rounded-2xl border border-dashed border-border/60 py-20 text-center">
          <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-secondary/60 text-muted-foreground">
            <Users className="h-5 w-5" />
          </div>
          <div>
            <p className="font-medium">No leads yet</p>
            <p className="mt-1 max-w-sm text-sm text-muted-foreground">
              A prospect becomes a lead once a campaign is built and its Apollo contact is
              created — not merely from appearing in a search result.
            </p>
          </div>
          <Link href="/" className={cn(buttonVariants({ size: "sm" }), "mt-1")}>
            Create a campaign
          </Link>
        </div>
      )}

      {!error && leads !== null && leads.length > 0 && (
        <div className="overflow-hidden rounded-xl border border-border/60">
          <table className="w-full text-sm">
            <thead className="bg-secondary/40 text-xs text-muted-foreground">
              <tr>
                <th className="px-3 py-2 text-left font-medium">Name</th>
                <th className="px-3 py-2 text-left font-medium">Company</th>
                <th className="px-3 py-2 text-left font-medium">Title</th>
                <th className="px-3 py-2 text-left font-medium">Email</th>
                <th className="px-3 py-2 text-left font-medium">Status</th>
                <th className="px-3 py-2 text-right font-medium">Campaigns</th>
                <th className="px-3 py-2 text-right font-medium">Created</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border/60">
              {leads.map((lead) => (
                <tr key={lead.lead_id} className="cursor-pointer hover:bg-secondary/30">
                  <td className="p-0">
                    <Link href={`/manager/leads/${lead.lead_id}`} className="block px-3 py-2.5">
                      {leadName(lead)}
                    </Link>
                  </td>
                  <td className="px-3 py-2.5 text-muted-foreground">{lead.company ?? "—"}</td>
                  <td className="px-3 py-2.5 text-muted-foreground">{lead.title ?? "—"}</td>
                  <td className="px-3 py-2.5 text-muted-foreground">{lead.email ?? "—"}</td>
                  <td className="px-3 py-2.5">
                    <Badge variant="outline" className="rounded-full font-normal">
                      {lead.status}
                    </Badge>
                  </td>
                  <td className="px-3 py-2.5 text-right text-muted-foreground">
                    {lead.campaign_count}
                  </td>
                  <td className="px-3 py-2.5 text-right text-muted-foreground">
                    {formatDate(lead.created_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
