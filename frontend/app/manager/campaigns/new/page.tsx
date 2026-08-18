"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { ArrowRight, Mail, Sparkles } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { CreateMailCampaignModal } from "@/components/create-mail-campaign-modal";
import type { MailCampaign } from "@/lib/api";

// Method-first fork for Campaign Manager's "Create Campaign". Apollo's own
// creation workflow (the Claude prompt at "/") is untouched by this page --
// choosing it just navigates there, no campaign-name field or other step is
// introduced in front of it. Astronomic Mail opens the Create Campaign
// modal (campaign-level sending-rule configuration) instead of the old
// inline "name a campaign" mini-form.
export default function NewCampaignPage() {
  const router = useRouter();
  const [mailModalOpen, setMailModalOpen] = useState(false);

  function chooseApollo() {
    router.push("/");
  }

  function handleMailCampaignCreated(campaign: MailCampaign) {
    setMailModalOpen(false);
    router.push(`/manager/campaigns/mail/${campaign.mail_campaign_id}`);
  }

  return (
    <div className="mx-auto max-w-2xl px-6 py-10">
      <div className="mb-8">
        <h1 className="mb-2 font-serif text-2xl font-medium tracking-tight">Create a campaign</h1>
        <p className="text-sm text-muted-foreground">Choose Sending Method to get started.</p>
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

        <Card className="cursor-pointer transition-colors hover:bg-secondary/40" onClick={chooseApollo}>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <Sparkles className="h-4 w-4" />
              Apollo
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-muted-foreground">Use the existing Apollo campaign workflow.</p>
            <p className="mt-2 flex items-center gap-1 text-xs text-muted-foreground/70">
              Continue to Astro <ArrowRight className="h-3 w-3" />
            </p>
          </CardContent>
        </Card>
      </div>

      <CreateMailCampaignModal open={mailModalOpen} onOpenChange={setMailModalOpen} onCreated={handleMailCampaignCreated} />
    </div>
  );
}
