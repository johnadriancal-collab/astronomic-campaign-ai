"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { ArrowRight, Loader2, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { previewCampaign, ApiError } from "@/lib/api";
import { useCampaignStore } from "@/lib/store";

// Relocated from "/" -- this is Apollo's single-shot Claude plan-generation
// prompt, reached only via Campaign Manager's "Create Campaign -> Apollo"
// choice (see app/manager/campaigns/new/page.tsx), never as a top-level
// nav item. Copy here deliberately avoids "Astro"/"Ask Astro" language so
// it can't be mistaken for the separate, general-purpose Astro AI
// assistant at /astro-ai -- that confusion (a user typing a plain question
// here and getting an Apollo campaign back) is exactly what this move
// fixes.
const EXAMPLE_PROMPTS = ["Create a campaign", "Find investors", "Check a prospect", "Analyze campaign"];

export default function CampaignBuilderPage() {
  const router = useRouter();
  const setCampaign = useCampaignStore((s) => s.setCampaign);
  const desiredProspectCount = useCampaignStore((s) => s.desiredProspectCount);

  const [value, setValue] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleGenerate() {
    if (!value.trim() || loading) return;
    setLoading(true);
    setError(null);
    try {
      // The ONLY Claude plan-generation call in the whole flow -- every
      // later stage reuses this campaign_id instead of regenerating.
      const campaign = await previewCampaign(value.trim(), desiredProspectCount);
      setCampaign(campaign);
      router.push("/results");
    } catch (err) {
      setError(
        err instanceof ApiError
          ? `Claude couldn't generate a plan (${err.status}): ${err.message}`
          : "Couldn't reach the backend. Is it running on localhost:8000?"
      );
      setLoading(false);
    }
  }

  return (
    <div className="hero-glow flex min-h-[calc(100vh-3.5rem)] flex-col items-center justify-center px-6 py-16">
      <div className="w-full max-w-2xl">
        <div className="mb-8 flex flex-col items-center text-center animate-in fade-in slide-in-from-bottom-2 duration-700">
          <span className="mb-4 inline-flex items-center gap-1.5 rounded-full border border-border/60 bg-secondary/60 px-3 py-1 text-xs text-muted-foreground">
            <Sparkles className="h-3 w-3 text-primary" />
            APOLLO CAMPAIGN BUILDER
          </span>

          <h1 className="text-balance font-serif text-4xl font-medium tracking-tight sm:text-5xl">
            Describe the campaign you want to build
          </h1>
          <p className="mt-4 max-w-md text-balance text-sm text-muted-foreground sm:text-base">
            Astronomic will search Apollo, build your audience, and draft an outreach sequence.
          </p>
        </div>

        <div className="animate-in fade-in slide-in-from-bottom-4 duration-700 [animation-delay:100ms]">
          <div className="rounded-lg border border-border bg-card p-3 shadow-sm transition-colors focus-within:border-primary/50">
            <Textarea
              value={value}
              onChange={(e) => setValue(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                  e.preventDefault();
                  handleGenerate();
                }
              }}
              placeholder="Describe who you want to reach..."
              disabled={loading}
              rows={5}
              className="resize-none border-none bg-transparent p-2 text-base shadow-none focus-visible:ring-0 md:text-base"
            />
            <div className="flex items-center justify-between px-2 pb-1 pt-2">
              <span className="text-xs text-muted-foreground">⌘ + Enter to generate</span>
              <Button onClick={handleGenerate} disabled={!value.trim() || loading} size="sm" className="gap-1.5">
                {loading ? (
                  <>
                    <Loader2 className="h-4 w-4 animate-spin" />
                    Generating…
                  </>
                ) : (
                  <>
                    Generate campaign
                    <ArrowRight className="h-4 w-4" />
                  </>
                )}
              </Button>
            </div>
          </div>

          {error && (
            <Alert variant="destructive" className="mt-4">
              <AlertTitle>Something went wrong</AlertTitle>
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}

          {!loading && (
            <div className="mt-5 flex flex-wrap justify-center gap-2">
              {EXAMPLE_PROMPTS.map((example) => (
                <button
                  key={example}
                  onClick={() => setValue(example)}
                  className="rounded-full border border-border/60 bg-secondary/40 px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:border-primary/40 hover:text-foreground"
                >
                  {example}
                </button>
              ))}
            </div>
          )}

          {loading && (
            <div className="mt-6 flex items-center justify-center gap-2 text-sm text-muted-foreground animate-in fade-in duration-500">
              <span className="relative flex h-2 w-2">
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-primary opacity-60" />
                <span className="relative inline-flex h-2 w-2 rounded-full bg-primary" />
              </span>
              Building your campaign…
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
