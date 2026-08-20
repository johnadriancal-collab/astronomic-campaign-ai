"use client";

import { useState } from "react";
import { ArrowRight, Loader2, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { ApiError, sendAstroChatMessage, type AstroChatMessage } from "@/lib/api";
import { appendUserMessage, canSubmit, shouldSubmitOnKeyDown } from "@/lib/astro-ai-chat";
import { cn } from "@/lib/utils";

// This is the Hub's one Astro AI surface -- the original hero design,
// wired to the general-purpose Astro chat API (POST /astro-ai/chat)
// instead of Campaign Builder's plan-generation endpoint. Astro AI has no
// CRM/Apollo/campaign tools of its own yet; the Apollo plan generator
// stays isolated on its own relocated page, reached only via Campaign
// Manager's explicit "Create Campaign -> Apollo" choice (see
// app/manager/campaigns/new).
const EXAMPLE_PROMPTS = ["Create a campaign", "Find investors", "Check a prospect", "Analyze campaign"];

export default function Home() {
  const [messages, setMessages] = useState<AstroChatMessage[]>([]);
  const [value, setValue] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSend() {
    if (!canSubmit(value, loading)) return;

    const nextMessages = appendUserMessage(messages, value);
    setMessages(nextMessages);
    setValue("");
    setError(null);
    setLoading(true);
    try {
      const reply = await sendAstroChatMessage(nextMessages);
      setMessages((prev) => [...prev, reply]);
    } catch (err) {
      setError(
        err instanceof ApiError ? `Couldn't reach Astro AI (${err.status}): ${err.message}` : "Couldn't reach the backend."
      );
    } finally {
      setLoading(false);
    }
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (shouldSubmitOnKeyDown(e.key, e.shiftKey)) {
      e.preventDefault();
      handleSend();
    }
    // Shift+Enter falls through to the textarea's own default (newline).
  }

  return (
    <div className="hero-glow flex min-h-[calc(100vh-3.5rem)] flex-col items-center justify-center px-6 py-16">
      <div className="w-full max-w-2xl">
        <div className="mb-8 flex flex-col items-center text-center animate-in fade-in slide-in-from-bottom-2 duration-700">
          <span className="mb-4 inline-flex items-center gap-1.5 rounded-full border border-border/60 bg-secondary/60 px-3 py-1 text-xs text-muted-foreground">
            <Sparkles className="h-3 w-3 text-primary" />
            ASTRONOMIC INTELLIGENCE
          </span>

          <h1 className="text-balance font-serif text-4xl font-medium tracking-tight sm:text-5xl">
            What can Astro do for you?
          </h1>
          <p className="mt-4 max-w-md text-balance text-sm text-muted-foreground sm:text-base">
            Create campaigns, find prospects, analyze your CRM, check campaign performance, and
            more.
          </p>
        </div>

        <div className="animate-in fade-in slide-in-from-bottom-4 duration-700 [animation-delay:100ms]">
          <div className="rounded-lg border border-border bg-card p-3 shadow-sm transition-colors focus-within:border-primary/50">
            {(messages.length > 0 || loading) && (
              <div className="mb-3 flex max-h-96 flex-col gap-3 overflow-y-auto">
                {messages.map((m, i) => (
                  <div key={i} className={cn("flex", m.role === "user" ? "justify-end" : "justify-start")}>
                    <div
                      className={cn(
                        "max-w-[85%] whitespace-pre-wrap rounded-lg px-3 py-2 text-sm",
                        m.role === "user" ? "bg-primary text-primary-foreground" : "bg-secondary/60 text-foreground"
                      )}
                    >
                      {m.content}
                    </div>
                  </div>
                ))}
                {loading && (
                  <div className="flex items-center gap-2 self-start rounded-lg bg-secondary/60 px-3 py-2 text-sm text-muted-foreground">
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    Astro is thinking…
                  </div>
                )}
              </div>
            )}

            <Textarea
              value={value}
              onChange={(e) => setValue(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Ask Astro anything..."
              disabled={loading}
              rows={5}
              className="resize-none border-none bg-transparent p-2 text-base shadow-none focus-visible:ring-0 md:text-base"
            />
            <div className="flex items-center justify-between px-2 pb-1 pt-2">
              <span className="text-xs text-muted-foreground">Enter to send &middot; Shift+Enter for a new line</span>
              <Button onClick={handleSend} disabled={!canSubmit(value, loading)} size="sm" className="gap-1.5">
                {loading ? (
                  <>
                    <Loader2 className="h-4 w-4 animate-spin" />
                    Thinking…
                  </>
                ) : (
                  <>
                    Ask Astro
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

          {messages.length === 0 && !loading && (
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
        </div>
      </div>
    </div>
  );
}
