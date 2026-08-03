"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { ArrowRight, Loader2, Sparkles, Users } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Input } from "@/components/ui/input";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { previewCampaign, ApiError } from "@/lib/api";
import { useCampaignStore } from "@/lib/store";

const EXAMPLE_PROMPTS = [
  "Create an investor dinner campaign for FlexRadio. Location: San Francisco. Audience: early-stage technology investors. Sequence: 4 emails. Tone: professional, conversational, not salesy. Delay: 3 days between emails.",
  "Outreach to Series A fintech founders in New York for a private roundtable dinner. 3-email sequence, warm and curious tone, 4 days apart.",
  "Invite early-stage AI investors in Austin to a founder demo night. Friendly but credible tone, 4 emails spaced 2 days apart.",
];

export default function Home() {
  const router = useRouter();
  const setCampaign = useCampaignStore((s) => s.setCampaign);
  const desiredProspectCount = useCampaignStore((s) => s.desiredProspectCount);
  const setDesiredProspectCount = useCampaignStore((s) => s.setDesiredProspectCount);

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
            Claude + Apollo outreach automation
          </span>
          <h1 className="text-balance text-4xl font-semibold tracking-tight sm:text-5xl">
            Describe your campaign.
            <br />
            <span className="text-muted-foreground">Claude drafts it.</span>
          </h1>
          <p className="mt-4 max-w-md text-balance text-sm text-muted-foreground sm:text-base">
            Plain-English brief in, targeting filters and a full email sequence out —
            reviewed before anything ever reaches Apollo.
          </p>
        </div>

        <div className="animate-in fade-in slide-in-from-bottom-4 duration-700 [animation-delay:100ms]">
          <div className="rounded-2xl border border-border/60 bg-card/80 p-3 shadow-2xl shadow-black/20 backdrop-blur-sm transition-shadow focus-within:shadow-primary/10">
            <Textarea
              value={value}
              onChange={(e) => setValue(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                  e.preventDefault();
                  handleGenerate();
                }
              }}
              placeholder="Create an investor dinner campaign for FlexRadio. Location: San Francisco. Audience: early-stage technology investors. Sequence: 4 emails..."
              disabled={loading}
              rows={5}
              className="resize-none border-none bg-transparent p-2 text-base shadow-none focus-visible:ring-0 md:text-base"
            />
            <div className="flex items-center justify-between px-2 pb-1 pt-2">
              <div className="flex items-center gap-3">
                <span className="text-xs text-muted-foreground">⌘ + Enter to generate</span>
                <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
                  <Users className="h-3 w-3" />
                  Target prospects
                  <Input
                    type="number"
                    min={5}
                    max={200}
                    value={desiredProspectCount}
                    disabled={loading}
                    onChange={(e) => {
                      const n = parseInt(e.target.value, 10);
                      setDesiredProspectCount(Number.isFinite(n) ? n : 25);
                    }}
                    className="h-6 w-16 rounded-md border-border/60 bg-secondary/40 px-2 text-xs"
                  />
                </label>
              </div>
              <Button onClick={handleGenerate} disabled={!value.trim() || loading} size="sm" className="gap-1.5">
                {loading ? (
                  <>
                    <Loader2 className="h-4 w-4 animate-spin" />
                    Generating…
                  </>
                ) : (
                  <>
                    Generate Campaign
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
                  {example.slice(0, 46)}…
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
              Claude is drafting targeting filters and a 4-email sequence…
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
