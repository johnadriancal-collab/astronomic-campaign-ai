"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { AlertTriangle, Megaphone, Users } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { CampaignStatusBadge } from "@/components/campaign-status-badge";
import { ApiError, listCampaigns, type Campaign } from "@/lib/api";
import { cn } from "@/lib/utils";

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

export default function CampaignsPage() {
  const [campaigns, setCampaigns] = useState<Campaign[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    (async () => {
      setError(null);
      try {
        const data = await listCampaigns();
        if (!cancelled) setCampaigns(data);
      } catch (err) {
        if (!cancelled) {
          setError(
            err instanceof ApiError
              ? `Couldn't load campaigns (${err.status}): ${err.message}`
              : "Couldn't reach the backend to load campaigns."
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
        <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">Campaigns</h1>
        <p className="mt-2 max-w-2xl text-sm text-muted-foreground">
          Every campaign created in Campaign Builder, loaded from the persistent campaign store.
        </p>
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
              Campaigns you create in Campaign Builder will show up here once they exist.
            </p>
          </div>
          <Link href="/" className={cn(buttonVariants({ size: "sm" }), "mt-1")}>
            Create a campaign
          </Link>
        </div>
      )}

      {!error && campaigns !== null && campaigns.length > 0 && (
        <div className="grid gap-4 sm:grid-cols-2">
          {campaigns.map((campaign) => (
            <Link key={campaign.campaign_id} href={`/manager/campaigns/${campaign.campaign_id}`}>
              <Card className="h-full transition-colors hover:bg-secondary/40">
                <CardHeader>
                  <div className="mb-1 flex items-start justify-between gap-2">
                    <CardTitle className="leading-snug">{campaign.plan.campaign_name}</CardTitle>
                    <CampaignStatusBadge status={campaign.status} />
                  </div>
                  <p className="line-clamp-2 text-sm text-muted-foreground">
                    {campaign.original_prompt}
                  </p>
                </CardHeader>
                <CardContent className="flex flex-wrap items-center gap-1.5">
                  {campaign.total_matches !== null && (
                    <Badge
                      variant="outline"
                      className="gap-1 rounded-full border-border/60 font-normal text-muted-foreground"
                    >
                      <Users className="h-3 w-3" />
                      {campaign.total_matches.toLocaleString()} matches
                    </Badge>
                  )}
                  {campaign.status !== "draft" && (
                    <Badge variant="outline" className="rounded-full border-border/60 font-normal text-muted-foreground">
                      Selected: {campaign.selected_prospect_count}
                    </Badge>
                  )}
                  <span className="ml-auto text-xs text-muted-foreground/70">
                    {formatDate(campaign.created_at)}
                  </span>
                </CardContent>
              </Card>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
