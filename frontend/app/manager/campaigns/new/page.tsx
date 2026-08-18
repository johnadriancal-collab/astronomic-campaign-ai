"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { ArrowRight, Mail, Plus, Sparkles } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { ApiError, createMailCampaign } from "@/lib/api";

type Method = "apollo" | "astronomic_mail" | null;

// Method-first fork for Campaign Manager's "Create Campaign". Apollo's own
// creation workflow (the Claude prompt at "/") is untouched by this page --
// choosing it just navigates there, no campaign-name field or other step is
// introduced in front of it. Astronomic Mail reuses the exact "name a
// campaign" form that already lived on the old /crm/mail/campaigns page.
export default function NewCampaignPage() {
  const router = useRouter();
  const [method, setMethod] = useState<Method>(null);

  const [name, setName] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  function chooseApollo() {
    router.push("/");
  }

  async function handleCreateMailCampaign(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setCreating(true);
    setCreateError(null);
    try {
      const created = await createMailCampaign(name.trim());
      router.push(`/manager/campaigns/mail/${created.mail_campaign_id}`);
    } catch (err) {
      setCreateError(err instanceof ApiError ? `Couldn't create campaign (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setCreating(false);
    }
  }

  return (
    <div className="mx-auto max-w-2xl px-6 py-10">
      <div className="mb-8">
        <h1 className="mb-2 font-serif text-2xl font-medium tracking-tight">Create a campaign</h1>
        <p className="text-sm text-muted-foreground">Choose Sending Method to get started.</p>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <Card
          className={`cursor-pointer transition-colors hover:bg-secondary/40 ${method === "astronomic_mail" ? "border-primary" : ""}`}
          onClick={() => setMethod("astronomic_mail")}
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

      {method === "astronomic_mail" && (
        <Card className="mt-6">
          <CardHeader>
            <CardTitle className="text-sm">Name this campaign</CardTitle>
          </CardHeader>
          <CardContent>
            {createError && (
              <Alert variant="destructive" className="mb-3">
                <AlertDescription>{createError}</AlertDescription>
              </Alert>
            )}
            <form onSubmit={handleCreateMailCampaign} className="flex gap-2">
              <Input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Q1 Investor Outreach"
                autoFocus
                required
              />
              <Button type="submit" disabled={creating || !name.trim()} className="shrink-0 gap-1.5">
                <Plus className="h-4 w-4" />
                {creating ? "Creating..." : "Create draft"}
              </Button>
            </form>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
