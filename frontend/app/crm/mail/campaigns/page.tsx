"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { AlertTriangle, Mail, Plus } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { ApiError, createMailCampaign, listMailCampaigns, type MailCampaign } from "@/lib/api";
import { formatScheduleSummary, mailCampaignStatusBadgeClass, mailCampaignStatusLabel } from "@/lib/mail";
import { cn } from "@/lib/utils";

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  } catch {
    return iso;
  }
}

export default function MailCampaignsPage() {
  const router = useRouter();
  const [campaigns, setCampaigns] = useState<MailCampaign[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [name, setName] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  async function load() {
    try {
      setCampaigns(await listMailCampaigns());
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? `Couldn't load campaigns (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    }
  }

  useEffect(() => {
    load();
  }, []);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setCreating(true);
    setCreateError(null);
    try {
      const created = await createMailCampaign(name.trim());
      router.push(`/crm/mail/campaigns/${created.mail_campaign_id}`);
    } catch (err) {
      setCreateError(err instanceof ApiError ? `Couldn't create campaign (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setCreating(false);
    }
  }

  return (
    <div className="mx-auto max-w-3xl px-6 py-10">
      <div className="mb-6">
        <h1 className="mb-2 font-serif text-2xl font-medium tracking-tight">Mail Campaigns</h1>
        <p className="text-sm text-muted-foreground">
          Astronomic Mail (Phase 1). Build and review a campaign against an existing CRM List -- there is no sending
          yet. Mailbox setup and sending will be added in a later phase.
        </p>
      </div>

      {error && (
        <Alert variant="destructive" className="mb-4">
          <AlertTriangle />
          <AlertTitle>Couldn&apos;t load campaigns</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      <Card className="mb-6">
        <CardHeader>
          <CardTitle className="text-sm">New campaign</CardTitle>
        </CardHeader>
        <CardContent>
          {createError && (
            <Alert variant="destructive" className="mb-3">
              <AlertDescription>{createError}</AlertDescription>
            </Alert>
          )}
          <form onSubmit={handleCreate} className="flex gap-2">
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Q1 Investor Outreach" required />
            <Button type="submit" disabled={creating || !name.trim()} className="shrink-0 gap-1.5">
              <Plus className="h-4 w-4" />
              {creating ? "Creating..." : "Create draft"}
            </Button>
          </form>
        </CardContent>
      </Card>

      {campaigns && (
        <div className="space-y-2">
          {campaigns.map((campaign) => (
            <Link key={campaign.mail_campaign_id} href={`/crm/mail/campaigns/${campaign.mail_campaign_id}`}>
              <Card className="transition-colors hover:bg-secondary/40">
                <CardContent className="py-3">
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <p className="flex items-center gap-1.5 text-sm font-medium">
                        <Mail className="h-3.5 w-3.5 text-muted-foreground" />
                        {campaign.name}
                      </p>
                      <p className="mt-1 text-xs text-muted-foreground">{formatScheduleSummary(campaign)}</p>
                      <p className="mt-1 text-xs text-muted-foreground">created {formatDate(campaign.created_at)}</p>
                    </div>
                    <span
                      className={cn(
                        "shrink-0 rounded-full px-2 py-0.5 text-xs font-medium",
                        mailCampaignStatusBadgeClass(campaign.status)
                      )}
                    >
                      {mailCampaignStatusLabel(campaign.status)}
                    </span>
                  </div>
                </CardContent>
              </Card>
            </Link>
          ))}
          {campaigns.length === 0 && (
            <p className="text-sm text-muted-foreground">No Mail Campaigns yet -- create one above.</p>
          )}
        </div>
      )}
    </div>
  );
}
