"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { AlertTriangle, Megaphone } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError, listUnifiedCampaigns, type UnifiedCampaignSummary } from "@/lib/api";
import { mailCampaignStatusBadgeClass, mailCampaignStatusLabel } from "@/lib/mail";
import type { MailCampaignStatus } from "@/lib/api";
import { cn } from "@/lib/utils";

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

// Campaign Manager's Apollo Campaign/Sequence integration is disabled --
// this endpoint now only ever returns astronomic_mail items (see
// app/api/campaign_manager.py), so there is exactly one status shape to
// render here. No sending-method badge is needed since there's only one.
function StatusBadge({ item }: { item: UnifiedCampaignSummary }) {
  const status = item.raw_status as MailCampaignStatus;
  return (
    <span className={cn("shrink-0 rounded-full px-2 py-0.5 text-xs font-medium", mailCampaignStatusBadgeClass(status))}>
      {mailCampaignStatusLabel(status)}
    </span>
  );
}

export default function CampaignsPage() {
  const [campaigns, setCampaigns] = useState<UnifiedCampaignSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function loadCampaigns(): Promise<void> {
    try {
      const data = await listUnifiedCampaigns();
      setCampaigns(data);
      setError(null);
    } catch (err) {
      setError(
        err instanceof ApiError
          ? `Couldn't load campaigns (${err.status}): ${err.message}`
          : "Couldn't reach the backend to load campaigns."
      );
    }
  }

  useEffect(() => {
    let cancelled = false;

    (async () => {
      if (!cancelled) await loadCampaigns();
    })();

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="mx-auto max-w-5xl px-6 py-10">
      <div className="mb-8 flex items-start justify-between gap-4">
        <div>
          <h1 className="font-serif text-2xl font-medium tracking-tight sm:text-3xl">Campaigns</h1>
          <p className="mt-2 max-w-2xl text-sm text-muted-foreground">
            Create and manage your Astronomic Mail campaigns.
          </p>
        </div>
      </div>

      {error && (
        <Alert variant="destructive">
          <AlertTriangle />
          <AlertTitle>Couldn&apos;t load campaigns</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {!error && campaigns === null && (
        <div className="grid gap-4 sm:grid-cols-2">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-36 rounded-xl" />
          ))}
        </div>
      )}

      {!error && campaigns !== null && campaigns.length === 0 && (
        <div className="flex flex-col items-center gap-4 rounded-2xl border border-dashed border-border/60 py-20 text-center">
          <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-secondary/60 text-muted-foreground">
            <Megaphone className="h-5 w-5" />
          </div>
          <div>
            <p className="font-medium">No campaigns yet</p>
            <p className="mt-1 max-w-sm text-sm text-muted-foreground">
              Campaigns you create will show up here once they exist.
            </p>
          </div>
          <Link href="/manager/campaigns/new" className={cn(buttonVariants({ size: "sm" }), "mt-1")}>
            Create a campaign
          </Link>
        </div>
      )}

      {!error && campaigns !== null && campaigns.length > 0 && (
        <div className="grid gap-4 sm:grid-cols-2">
          {campaigns.map((item) => (
            <Link key={`${item.sending_method}-${item.id}`} href={item.detail_path}>
              <Card className="h-full transition-colors hover:bg-secondary/40">
                <CardHeader>
                  <div className="mb-1 flex items-start justify-between gap-2">
                    <CardTitle className="leading-snug">{item.name}</CardTitle>
                    <StatusBadge item={item} />
                  </div>
                </CardHeader>
                <CardContent className="flex flex-wrap items-center gap-1.5">
                  <span className="text-sm text-muted-foreground">{item.summary}</span>
                  <span className="ml-auto text-xs text-muted-foreground/70">{formatDate(item.created_at)}</span>
                </CardContent>
              </Card>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
