"use client";

import { useState } from "react";
import { Loader2, SendHorizontal, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ContactResults } from "@/components/crm-contact-results";
import { ApiError, sendAstroCommand } from "@/lib/api";
import {
  applyAstroResponse,
  composeAstroMessage,
  contextForRequest,
  INITIAL_ASTRO_CONVERSATION_STATE,
  type AstroConversationState,
} from "@/lib/astro-conversation";
import { cn } from "@/lib/utils";

interface ConversationMessage {
  role: "user" | "astro" | "error";
  text: string;
}

const EXAMPLE_PROMPTS = ["Find family offices in Texas", "Show institutional investors", "Only Austin", "How many are left?"];

export default function AstroSearchPage() {
  const [state, setState] = useState<AstroConversationState>(INITIAL_ASTRO_CONVERSATION_STATE);
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);

  async function runCommand(text: string) {
    if (!text.trim() || loading) return;
    setMessages((prev) => [...prev, { role: "user", text }]);
    setInput("");
    setLoading(true);
    try {
      const response = await sendAstroCommand(text, contextForRequest(state));
      setMessages((prev) => [...prev, { role: "astro", text: composeAstroMessage(response) }]);
      setState((prev) => applyAstroResponse(prev, response));
    } catch (err) {
      const errorText =
        err instanceof ApiError ? `Couldn't reach Astro (${err.status}): ${err.message}` : "Couldn't reach the backend. Is it running?";
      setMessages((prev) => [...prev, { role: "error", text: errorText }]);
    } finally {
      setLoading(false);
    }
  }

  function handleSubmit() {
    runCommand(input);
  }

  const contacts = state.contacts;
  const hasActiveFilters = Boolean(state.context?.query?.filters.length);

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-6 px-6 py-10">
      <div>
        <span className="mb-2 inline-flex items-center gap-1.5 rounded-full border border-border/60 bg-secondary/60 px-3 py-1 text-xs text-muted-foreground">
          <Sparkles className="h-3 w-3 text-primary" />
          DETERMINISTIC -- NO CLAUDE
        </span>
        <h1 className="font-serif text-2xl font-medium tracking-tight sm:text-3xl">Astro Search</h1>
        <p className="mt-2 max-w-2xl text-sm text-muted-foreground">
          Search and count your CRM contacts in plain English, then refine turn by turn. This is the CRM assistant, not the
          Apollo Campaign Builder -- read-only, no lists, no exports, no CRM writes.
        </p>
      </div>

      <div className="rounded-lg border border-border bg-card p-3 shadow-sm">
        <div className="flex flex-col gap-3">
          {messages.length === 0 && (
            <p className="px-1 py-2 text-sm text-muted-foreground">
              Try one of these, or ask your own question about your CRM contacts.
            </p>
          )}
          {messages.map((m, i) => (
            <div key={i} className={cn("flex", m.role === "user" ? "justify-end" : "justify-start")}>
              <div
                className={cn(
                  "max-w-[85%] rounded-lg px-3 py-2 text-sm",
                  m.role === "user" && "bg-primary text-primary-foreground",
                  m.role === "astro" && "bg-secondary/60 text-foreground",
                  m.role === "error" && "bg-destructive/10 text-destructive"
                )}
              >
                {m.text}
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

        <form
          onSubmit={(e) => {
            e.preventDefault();
            handleSubmit();
          }}
          className="mt-3 flex items-center gap-2"
        >
          <Input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key !== "Enter") return;
              e.preventDefault();
              handleSubmit();
            }}
            placeholder="Find family offices in Texas..."
            disabled={loading}
          />
          <Button type="submit" size="sm" disabled={!input.trim() || loading} className="gap-1.5">
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

      {state.total !== null && (
        <div>
          <p className="mb-3 text-sm text-muted-foreground">
            {state.total} total match{state.total === 1 ? "" : "es"}
            {contacts && contacts.length < state.total ? ` (showing the first ${contacts.length})` : ""}
          </p>
          <ContactResults
            contacts={contacts}
            total={contacts?.length ?? 0}
            error={null}
            hasActiveFilters={hasActiveFilters}
            onClearFilters={() => runCommand("Start over")}
            simple
          />
        </div>
      )}
    </div>
  );
}
