// Pure logic for the Astro AI chat page -- kept separate from the page
// component so it's unit-testable without rendering React, same split as
// lib/astro-conversation.ts and lib/contact-selection.ts.

import type { AstroChatMessage } from "@/lib/api";

// The single source of truth for "is Send/Enter allowed right now" -- used
// both by the Send button's `disabled` and by the Enter-key handler, so
// there is exactly one place that can accidentally allow a duplicate
// concurrent submission.
export function canSubmit(input: string, loading: boolean): boolean {
  return input.trim().length > 0 && !loading;
}

export function appendUserMessage(messages: AstroChatMessage[], content: string): AstroChatMessage[] {
  return [...messages, { role: "user", content: content.trim() }];
}

// Enter sends; Shift+Enter is left alone so the textarea's own default
// (insert a newline) happens instead.
export function shouldSubmitOnKeyDown(key: string, shiftKey: boolean): boolean {
  return key === "Enter" && !shiftKey;
}
