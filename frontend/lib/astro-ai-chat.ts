// Pure logic for the Astro AI chat page -- kept separate from the page
// component so it's unit-testable without rendering React, same split as
// lib/astro-conversation.ts and lib/contact-selection.ts.

import type { AstroChatAttachment, AstroChatMessage } from "@/lib/api";

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

// --- CSV export attachment ---------------------------------------------

// TypeScript's AstroChatAttachment type is erased at runtime -- it's not a
// guarantee about what the backend actually sent, just what we expect. A
// partial/malformed attachment (a backend bug, not a real contract this
// schema should produce) must never render a broken download control --
// it should just be treated as if there were no attachment at all.
export function isRenderableAttachment(
  attachment: AstroChatAttachment | null | undefined
): attachment is AstroChatAttachment {
  if (!attachment) return false;
  return (
    typeof attachment.filename === "string" &&
    attachment.filename.length > 0 &&
    typeof attachment.url === "string" &&
    attachment.url.length > 0 &&
    typeof attachment.contact_count === "number" &&
    attachment.contact_count >= 0
  );
}

// attachment.url is already the full authenticated, short-lived export path
// the backend generated (e.g. "/astro-ai/exports/<uuid>") -- this only adds
// the same "/backend" proxy prefix every other API call in this app already
// uses. Never constructs a public or permanent URL of its own.
export function buildDownloadHref(attachment: AstroChatAttachment): string {
  return `/backend${attachment.url}`;
}

export const EXPORT_EXPIRED_MESSAGE = "This export has expired -- ask Astro to export it again.";
export const EXPORT_DOWNLOAD_FAILED_MESSAGE = "Couldn't download this export -- please try again.";

export type AttachmentDownloadResult = { ok: true; blob: Blob } | { ok: false; message: string };

// Fetches the CSV through the same authenticated/expiring endpoint the
// plain href would have hit -- this exists only to turn a 404 (expired) or
// network failure into a clean in-app message instead of a raw failed
// browser navigation. `fetchImpl` is injectable so this is testable without
// a real network call or a DOM.
export async function fetchAttachmentBlob(
  href: string,
  fetchImpl: typeof fetch = fetch
): Promise<AttachmentDownloadResult> {
  try {
    const res = await fetchImpl(href);
    if (res.status === 404) {
      return { ok: false, message: EXPORT_EXPIRED_MESSAGE };
    }
    if (!res.ok) {
      return { ok: false, message: EXPORT_DOWNLOAD_FAILED_MESSAGE };
    }
    return { ok: true, blob: await res.blob() };
  } catch {
    return { ok: false, message: EXPORT_DOWNLOAD_FAILED_MESSAGE };
  }
}
