"use client";

import { useState } from "react";
import { Loader2, SendHorizontal, Sparkles } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, sendAstroChatMessage, type AstroChatMessage } from "@/lib/api";
import { appendUserMessage, canSubmit, shouldSubmitOnKeyDown } from "@/lib/astro-ai-chat";
import { cn } from "@/lib/utils";

// Astro AI -- Phase 1 general assistant foundation. Deliberately distinct
// from the Campaign Builder prompt at "/" (single-shot Claude JSON plan
// generation) and from Astro Search at /crm/astro (deterministic, no
// Claude, CRM-only) -- this is a genuine multi-turn Claude conversation
// with NO Hub-data access yet. Stateless like Astro Search: conversation
// state lives only in this page's React state, resent in full on every
// turn, and is lost on navigation/refresh by design (see the backend's
// astro_ai_service.py docstring for why a bigger persistence layer isn't
// built yet).
const EXAMPLE_PROMPTS = [
  "What is a family office?",
  "Write an invitation email for a VC partner to attend an AI founder dinner.",
  "Explain the difference between a SAFE and a convertible note.",
];

export default function AstroAiPage() {
  const [messages, setMessages] = useState<AstroChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSend() {
    // canSubmit() is the single source of truth for "is this allowed right
    // now" -- Enter and the Send button both funnel through this one
    // function, and both are also visibly disabled below while a request
    // is in flight, so there is no path to a duplicate concurrent send.
    if (!canSubmit(input, loading)) return;

    const nextMessages = appendUserMessage(messages, input);
    setMessages(nextMessages);
    setInput("");
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
    <div className="mx-auto flex max-w-3xl flex-col gap-6 px-6 py-10">
      <div>
        <span className="mb-2 inline-flex items-center gap-1.5 rounded-full border border-border/60 bg-secondary/60 px-3 py-1 text-xs text-muted-foreground">
          <Sparkles className="h-3 w-3 text-primary" />
          ASTRO AI
        </span>
        <h1 className="font-serif text-2xl font-medium tracking-tight sm:text-3xl">Astro AI</h1>
        <p className="mt-2 max-w-2xl text-sm text-muted-foreground">
          Ask Astro anything -- research, writing, analysis, and general business questions. Astro doesn&apos;t have
          access to Astronomic&apos;s CRM, campaigns, or mailboxes yet.
        </p>
      </div>

      <div className="rounded-lg border border-border bg-card p-3 shadow-sm">
        <div className="flex flex-col gap-3">
          {messages.length === 0 && (
            <p className="px-1 py-2 text-sm text-muted-foreground">
              Try one of these, or ask your own question.
            </p>
          )}
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

        {error && (
          <Alert variant="destructive" className="mt-3">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}

        <form
          onSubmit={(e) => {
            e.preventDefault();
            handleSend();
          }}
          className="mt-3 flex items-end gap-2"
        >
          <Textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Ask Astro anything... (Shift+Enter for a new line)"
            disabled={loading}
            rows={2}
            className="resize-none"
          />
          <Button type="submit" size="sm" disabled={!canSubmit(input, loading)} className="shrink-0 gap-1.5">
            <SendHorizontal className="h-4 w-4" />
            Send
          </Button>
        </form>

        {messages.length === 0 && (
          <div className="mt-3 flex flex-wrap gap-2">
            {EXAMPLE_PROMPTS.map((example) => (
              <button
                key={example}
                type="button"
                onClick={() => setInput(example)}
                className="rounded-full border border-border/60 bg-secondary/40 px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:border-primary/40 hover:text-foreground"
              >
                {example}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
