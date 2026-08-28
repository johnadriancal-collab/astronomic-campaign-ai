import assert from "node:assert/strict";
import { test } from "node:test";
import {
  appendUserMessage,
  buildDownloadHref,
  canSubmit,
  EXPORT_DOWNLOAD_FAILED_MESSAGE,
  EXPORT_EXPIRED_MESSAGE,
  fetchAttachmentBlob,
  isRenderableAttachment,
  shouldSubmitOnKeyDown,
} from "./astro-ai-chat.ts";

// --- canSubmit -- the single duplicate-submission gate ----------------------

test("canSubmit is true for non-empty input while not loading", () => {
  assert.equal(canSubmit("What is a family office?", false), true);
});

test("canSubmit is false while a request is already loading", () => {
  assert.equal(canSubmit("another question", true), false);
});

test("canSubmit is false for empty input", () => {
  assert.equal(canSubmit("", false), false);
});

test("canSubmit is false for whitespace-only input", () => {
  assert.equal(canSubmit("   \n  ", false), false);
});

test("canSubmit is false when both blank input AND loading", () => {
  assert.equal(canSubmit("", true), false);
});

// --- appendUserMessage -------------------------------------------------------

test("appendUserMessage adds a trimmed user message to an empty conversation", () => {
  const result = appendUserMessage([], "  Hello Astro  ");

  assert.deepEqual(result, [{ role: "user", content: "Hello Astro" }]);
});

test("appendUserMessage preserves existing history and appends after it", () => {
  const history = [
    { role: "user" as const, content: "What is a family office?" },
    { role: "assistant" as const, content: "A private wealth management firm." },
  ];

  const result = appendUserMessage(history, "Give an example");

  assert.deepEqual(result, [
    ...history,
    { role: "user", content: "Give an example" },
  ]);
});

test("appendUserMessage does not mutate the original array", () => {
  const history = [{ role: "user" as const, content: "hi" }];

  appendUserMessage(history, "another message");

  assert.equal(history.length, 1);
});

// --- shouldSubmitOnKeyDown ---------------------------------------------------

test("Enter without Shift submits", () => {
  assert.equal(shouldSubmitOnKeyDown("Enter", false), true);
});

test("Shift+Enter does not submit (inserts a newline instead)", () => {
  assert.equal(shouldSubmitOnKeyDown("Enter", true), false);
});

test("any other key never submits", () => {
  assert.equal(shouldSubmitOnKeyDown("a", false), false);
  assert.equal(shouldSubmitOnKeyDown("Tab", false), false);
  assert.equal(shouldSubmitOnKeyDown("Escape", true), false);
});

// --- isRenderableAttachment ---------------------------------------------
// This is the pure decision behind "does the chat show a visible download
// control" -- the page component (no test-rendering harness exists in this
// project; see package.json's `test` script, which runs plain lib/*.test.ts
// files, never a DOM) just calls this and renders (or doesn't) accordingly.

test("a real backend attachment is renderable (1: visible download control)", () => {
  assert.equal(
    isRenderableAttachment({ filename: "austin-angel-investors.csv", url: "/astro-ai/exports/abc-123", contact_count: 290 }),
    true
  );
});

test("filename is part of what makes an attachment renderable (2: filename displayed)", () => {
  const attachment = { filename: "all-contacts.csv", url: "/astro-ai/exports/xyz", contact_count: 1 };
  assert.equal(isRenderableAttachment(attachment), true);
  assert.equal(attachment.filename, "all-contacts.csv"); // this is exactly what the page renders as the card title
});

test("no attachment on a normal response is not renderable (4: unchanged for plain replies)", () => {
  assert.equal(isRenderableAttachment(null), false);
  assert.equal(isRenderableAttachment(undefined), false);
});

test("malformed/missing attachment fields are never renderable (5: doesn't crash the chat)", () => {
  assert.equal(isRenderableAttachment({ filename: "", url: "/astro-ai/exports/x", contact_count: 1 }), false);
  assert.equal(isRenderableAttachment({ filename: "x.csv", url: "", contact_count: 1 }), false);
  // @ts-expect-error -- deliberately wrong shape, simulating a malformed/partial backend payload
  assert.equal(isRenderableAttachment({ filename: "x.csv", url: "/astro-ai/exports/x" }), false);
  // @ts-expect-error -- contact_count as a string, not a number
  assert.equal(isRenderableAttachment({ filename: "x.csv", url: "/astro-ai/exports/x", contact_count: "290" }), false);
  // @ts-expect-error -- attachment isn't even an object
  assert.equal(isRenderableAttachment("not an attachment"), false);
});

// --- buildDownloadHref (3: attachment URL preserved backend -> frontend) ---

test("buildDownloadHref prefixes the backend's own export path unchanged", () => {
  const href = buildDownloadHref({ filename: "x.csv", url: "/astro-ai/exports/42425178-74f5-48a3-b3d7-3098acba2fee", contact_count: 290 });
  assert.equal(href, "/backend/astro-ai/exports/42425178-74f5-48a3-b3d7-3098acba2fee");
});

test("buildDownloadHref never invents a public/permanent URL of its own", () => {
  const href = buildDownloadHref({ filename: "x.csv", url: "/astro-ai/exports/some-id", contact_count: 1 });
  // Still the same authenticated, short-lived backend route -- just proxied.
  assert.match(href, /^\/backend\/astro-ai\/exports\//);
});

// --- fetchAttachmentBlob (expired/missing URLs fail gracefully) ------------

test("fetchAttachmentBlob returns the blob on a successful download", async () => {
  const fakeBlob = new Blob(["a,b\n1,2"]);
  const fakeFetch = async () => new Response(fakeBlob, { status: 200 });
  const result = await fetchAttachmentBlob("/backend/astro-ai/exports/abc", fakeFetch);
  assert.equal(result.ok, true);
});

test("fetchAttachmentBlob reports expiry distinctly on a 404", async () => {
  const fakeFetch = async () => new Response(null, { status: 404 });
  const result = await fetchAttachmentBlob("/backend/astro-ai/exports/expired", fakeFetch);
  assert.deepEqual(result, { ok: false, message: EXPORT_EXPIRED_MESSAGE });
});

test("fetchAttachmentBlob reports a generic failure on other error statuses", async () => {
  const fakeFetch = async () => new Response(null, { status: 500 });
  const result = await fetchAttachmentBlob("/backend/astro-ai/exports/x", fakeFetch);
  assert.deepEqual(result, { ok: false, message: EXPORT_DOWNLOAD_FAILED_MESSAGE });
});

test("fetchAttachmentBlob reports a generic failure on a network exception, never throws", async () => {
  const fakeFetch = async () => {
    throw new Error("network down");
  };
  const result = await fetchAttachmentBlob("/backend/astro-ai/exports/x", fakeFetch);
  assert.deepEqual(result, { ok: false, message: EXPORT_DOWNLOAD_FAILED_MESSAGE });
});
