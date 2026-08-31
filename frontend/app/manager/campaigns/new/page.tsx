"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Mail } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { CreateMailCampaignModal } from "@/components/create-mail-campaign-modal";
import type { MailCampaign } from "@/lib/api";

// The alternate sending-method card that used to sit alongside this one is
// disabled for now per current product direction, so this page no longer
// offers a method choice -- Astronomic Mail is the only creation path.
// That other card is intentionally removed from THIS page, not deleted
// from the app: its own destination route is untouched and still reachable
// directly if this decision is revisited.
export default function NewCampaignPage() {
  const router = useRouter();
  const [mailModalOpen, setMailModalOpen] = useState(false);

  function handleMailCampaignCreated(campaign: MailCampaign) {
    setMailModalOpen(false);
    router.push(`/manager/campaigns/mail/${campaign.mail_campaign_id}`);
  }

  return (
    <div className="mx-auto max-w-2xl px-6 py-10">
      <div className="mb-8">
        <h1 className="mb-2 font-serif text-2xl font-medium tracking-tight">Create a campaign</h1>
        <p className="text-sm text-muted-foreground">Set up a new Astronomic Mail campaign.</p>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <Card
          className="cursor-pointer transition-colors hover:bg-secondary/40"
          onClick={() => setMailModalOpen(true)}
        >
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <Mail className="h-4 w-4" />
              Astronomic Mail
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-muted-foreground">Send directly using our own sending infrastructure.</p>
          </CardContent>
        </Card>
      </div>

      <CreateMailCampaignModal open={mailModalOpen} onOpenChange={setMailModalOpen} onCreated={handleMailCampaignCreated} />
    </div>
  );
}
