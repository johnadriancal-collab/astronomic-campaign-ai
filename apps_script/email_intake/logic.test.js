const assert = require("node:assert/strict");
const { test } = require("node:test");
const {
  DEFAULT_MAX_BODY_CHARS,
  PERMANENT_FAILURE_STREAK_THRESHOLD,
  parseActivationInstant_,
  coarseSearchFloorDate_,
  isMessageEligibleByTime_,
  buildSearchQuery_,
  hasProcessedOrErrorLabel_,
  findHeader_,
  splitAddressList_,
  findBodyPart_,
  findAttachmentParts_,
  truncateBodyIfNeeded_,
  stripHtmlToPlainText_,
  selectBodyText_,
  shouldTreatPermanentStreakAsSystemic_,
  buildWebhookPayload_,
  classifyHttpOutcome_,
} = require("./logic.js");

// ---- parseActivationInstant_ -----------------------------------------------

test("parseActivationInstant_ parses a full timestamp with an offset", () => {
  const ms = parseActivationInstant_("2026-08-13T00:00:00+08:00");
  assert.equal(new Date(ms).toISOString(), "2026-08-12T16:00:00.000Z");
});

test("parseActivationInstant_ throws on a missing value (fail closed)", () => {
  assert.throws(() => parseActivationInstant_(undefined), /not set/);
  assert.throws(() => parseActivationInstant_(""), /not set/);
});

test("parseActivationInstant_ throws on a bare calendar date with no time", () => {
  assert.throws(() => parseActivationInstant_("2026-08-13"), /full timestamp/);
});

test("parseActivationInstant_ throws on an unparseable string", () => {
  assert.throws(() => parseActivationInstant_("T00:00 not-a-real-date"), /could not be parsed/);
});

// ---- coarseSearchFloorDate_ -------------------------------------------------

test("coarseSearchFloorDate_ is one day before the activation instant, never later", () => {
  const activationMs = new Date("2026-08-13T00:00:00Z").getTime();
  assert.equal(coarseSearchFloorDate_(activationMs), "2026/08/12");
});

// ---- isMessageEligibleByTime_ (test cases 1 and 2 from the audit checklist) --

test("message received before activation is not eligible", () => {
  const activationMs = new Date("2026-08-13T00:00:00Z").getTime();
  const beforeMs = new Date("2026-08-12T23:59:59Z").getTime();
  assert.equal(isMessageEligibleByTime_(beforeMs, activationMs), false);
});

test("message received exactly at activation is eligible", () => {
  const activationMs = new Date("2026-08-13T00:00:00Z").getTime();
  assert.equal(isMessageEligibleByTime_(activationMs, activationMs), true);
});

test("message received after activation is eligible", () => {
  const activationMs = new Date("2026-08-13T00:00:00Z").getTime();
  const afterMs = new Date("2026-08-13T00:00:01Z").getTime();
  assert.equal(isMessageEligibleByTime_(afterMs, activationMs), true);
});

test("message received 1ms before activation is not eligible", () => {
  const activationMs = new Date("2026-08-13T00:00:00.000Z").getTime();
  assert.equal(isMessageEligibleByTime_(activationMs - 1, activationMs), false);
});

test("message received exactly at activation, to the millisecond, is eligible", () => {
  const activationMs = new Date("2026-08-13T00:00:00.000Z").getTime();
  assert.equal(isMessageEligibleByTime_(activationMs, activationMs), true);
});

test("message received 1ms after activation is eligible", () => {
  const activationMs = new Date("2026-08-13T00:00:00.000Z").getTime();
  assert.equal(isMessageEligibleByTime_(activationMs + 1, activationMs), true);
});

test("isMessageEligibleByTime_ rejects a non-numeric timestamp safely", () => {
  const activationMs = new Date("2026-08-13T00:00:00Z").getTime();
  assert.equal(isMessageEligibleByTime_(NaN, activationMs), false);
  assert.equal(isMessageEligibleByTime_(undefined, activationMs), false);
});

test("old and new message in the same thread are judged independently by their own timestamps", () => {
  // Mirrors the audit's explicit thread scenario: one message before
  // activation, one after, in the SAME thread -- each message's own
  // internalDate decides eligibility, the thread id is irrelevant here.
  const activationMs = new Date("2026-08-13T00:00:00Z").getTime();
  const oldMessage = { threadId: "thread-1", internalDateMs: new Date("2026-08-01T00:00:00Z").getTime() };
  const newMessage = { threadId: "thread-1", internalDateMs: new Date("2026-08-14T00:00:00Z").getTime() };
  assert.equal(isMessageEligibleByTime_(oldMessage.internalDateMs, activationMs), false);
  assert.equal(isMessageEligibleByTime_(newMessage.internalDateMs, activationMs), true);
});

// ---- buildSearchQuery_ / hasProcessedOrErrorLabel_ (already-processed skip, test case 3) --

test("buildSearchQuery_ excludes both labels and floors one day before activation", () => {
  const activationMs = new Date("2026-08-13T00:00:00Z").getTime();
  assert.equal(
    buildSearchQuery_(activationMs, "crm-intake-processed", "crm-intake-error"),
    "-label:crm-intake-processed -label:crm-intake-error after:2026/08/12"
  );
});

test("hasProcessedOrErrorLabel_ is true when the message already carries the processed label", () => {
  assert.equal(hasProcessedOrErrorLabel_(["Label_1", "Label_2"], "Label_2", "Label_9"), true);
});

test("hasProcessedOrErrorLabel_ is true when the message already carries the error label", () => {
  assert.equal(hasProcessedOrErrorLabel_(["Label_1", "Label_9"], "Label_2", "Label_9"), true);
});

test("hasProcessedOrErrorLabel_ is false for an unlabeled eligible message, and handles missing labelIds safely", () => {
  assert.equal(hasProcessedOrErrorLabel_(["Label_1"], "Label_2", "Label_9"), false);
  assert.equal(hasProcessedOrErrorLabel_(undefined, "Label_2", "Label_9"), false);
  assert.equal(hasProcessedOrErrorLabel_(null, "Label_2", "Label_9"), false);
});

// ---- findHeader_ -------------------------------------------------------------

test("findHeader_ is case-insensitive and returns '' when absent", () => {
  const headers = [{ name: "Subject", value: "Hello" }, { name: "From", value: "a@x.com" }];
  assert.equal(findHeader_(headers, "subject"), "Hello");
  assert.equal(findHeader_(headers, "FROM"), "a@x.com");
  assert.equal(findHeader_(headers, "Cc"), "");
});

test("findHeader_ handles a missing/empty headers array safely (malformed message)", () => {
  assert.equal(findHeader_(null, "From"), "");
  assert.equal(findHeader_([], "From"), "");
});

// ---- splitAddressList_ -------------------------------------------------------

test("splitAddressList_ splits simple comma-separated addresses", () => {
  assert.deepEqual(splitAddressList_("a@x.com, b@y.com"), ["a@x.com", "b@y.com"]);
});

test("splitAddressList_ does not split on a comma inside a quoted display name", () => {
  assert.deepEqual(splitAddressList_('"Doe, Jane" <jane@x.com>, bob@y.com'), ['"Doe, Jane" <jane@x.com>', "bob@y.com"]);
});

test("splitAddressList_ returns an empty array for an empty header", () => {
  assert.deepEqual(splitAddressList_(""), []);
  assert.deepEqual(splitAddressList_(undefined), []);
});

// ---- findBodyPart_ / findAttachmentParts_ (MIME tree walking) --------------

test("findBodyPart_ finds a plain-text part on a simple non-multipart message", () => {
  const payload = { mimeType: "text/plain", body: { data: "aGVsbG8" } };
  assert.deepEqual(findBodyPart_(payload, "text/plain"), { mimeType: "text/plain", data: "aGVsbG8" });
});

test("findBodyPart_ finds a plain-text part nested inside multipart/mixed + multipart/alternative", () => {
  const payload = {
    mimeType: "multipart/mixed",
    parts: [
      {
        mimeType: "multipart/alternative",
        parts: [
          { mimeType: "text/plain", body: { data: "cGxhaW4tYm9keQ" } },
          { mimeType: "text/html", body: { data: "PGgxPmh0bWw8L2gxPg" } },
        ],
      },
      { mimeType: "application/pdf", filename: "deck.pdf", body: { size: 12345, attachmentId: "att1" } },
    ],
  };
  assert.deepEqual(findBodyPart_(payload, "text/plain"), { mimeType: "text/plain", data: "cGxhaW4tYm9keQ" });
});

test("findBodyPart_ returns null when no matching part exists (malformed/HTML-only message fails safely)", () => {
  const payload = { mimeType: "text/html", body: { data: "PGgxPg" } };
  assert.equal(findBodyPart_(payload, "text/plain"), null);
});

test("findAttachmentParts_ collects metadata only, never attachment content", () => {
  const payload = {
    mimeType: "multipart/mixed",
    parts: [
      { mimeType: "text/plain", body: { data: "aGVsbG8" } },
      { mimeType: "application/pdf", filename: "deck.pdf", body: { size: 12345, attachmentId: "att1" } },
      { mimeType: "image/png", filename: "logo.png", body: { size: 500, attachmentId: "att2" } },
    ],
  };
  const attachments = findAttachmentParts_(payload);
  assert.deepEqual(attachments, [
    { filename: "deck.pdf", content_type: "application/pdf", size_bytes: 12345 },
    { filename: "logo.png", content_type: "image/png", size_bytes: 500 },
  ]);
  // Explicitly confirms no attachmentId/content ever appears in the result.
  attachments.forEach((a) => assert.equal("attachmentId" in a, false));
});

test("findAttachmentParts_ returns an empty array when there are no attachments", () => {
  assert.deepEqual(findAttachmentParts_({ mimeType: "text/plain", body: { data: "aGVsbG8" } }), []);
});

// ---- truncateBodyIfNeeded_ ---------------------------------------------------

test("truncateBodyIfNeeded_ leaves a short body untouched", () => {
  assert.equal(truncateBodyIfNeeded_("hello", 100000), "hello");
});

test("truncateBodyIfNeeded_ truncates and appends a visible metadata marker, never silently", () => {
  const longText = "x".repeat(50);
  const result = truncateBodyIfNeeded_(longText, 10);
  assert.equal(result.startsWith("xxxxxxxxxx"), true);
  assert.match(result, /truncated by the Email Intake Apps Script transport/);
  assert.match(result, /original length: 50 characters/);
});

test("truncateBodyIfNeeded_ falls back to the documented default when maxChars is invalid", () => {
  const shortText = "hello";
  assert.equal(truncateBodyIfNeeded_(shortText, 0), shortText);
  assert.equal(truncateBodyIfNeeded_(shortText, -5), shortText);
  assert.equal(truncateBodyIfNeeded_(shortText, "not-a-number"), shortText);
});

test("truncateBodyIfNeeded_ handles an empty/missing body safely", () => {
  assert.equal(truncateBodyIfNeeded_("", 100), "");
  assert.equal(truncateBodyIfNeeded_(undefined, 100), "");
});

// ---- stripHtmlToPlainText_ / selectBodyText_ (HTML-only email handling, test cases 9-10) --

test("stripHtmlToPlainText_ strips tags and preserves line breaks at block boundaries", () => {
  const html = "<html><body><p>Hi Chris,</p><p>My new email is jane@x.com.</p></body></html>";
  assert.equal(stripHtmlToPlainText_(html), "Hi Chris,\nMy new email is jane@x.com.");
});

test("stripHtmlToPlainText_ removes script and style content entirely", () => {
  const html = "<style>.a{color:red}</style><p>Real text</p><script>doEvil()</script>";
  assert.equal(stripHtmlToPlainText_(html), "Real text");
});

test("stripHtmlToPlainText_ decodes common named and numeric entities", () => {
  assert.equal(stripHtmlToPlainText_("Tom &amp; Jerry &lt;fun&gt; &quot;time&quot;"), 'Tom & Jerry <fun> "time"');
  assert.equal(stripHtmlToPlainText_("A&#39;s &#x26; B&nbsp;"), "A's & B");
});

test("stripHtmlToPlainText_ leaves an unrecognized entity untouched rather than guessing", () => {
  assert.equal(stripHtmlToPlainText_("&madeupentity;"), "&madeupentity;");
});

test("stripHtmlToPlainText_ handles empty/missing/malformed input safely", () => {
  assert.equal(stripHtmlToPlainText_(""), "");
  assert.equal(stripHtmlToPlainText_(undefined), "");
  assert.equal(stripHtmlToPlainText_("<div><span>unclosed tags <p>here"), "unclosed tags here");
});

test("selectBodyText_ uses the plain-text part when one exists", () => {
  assert.equal(selectBodyText_(true, "plain body", true, "<p>html body</p>"), "plain body");
});

test("selectBodyText_ falls back to stripped HTML only when NO plain-text part exists", () => {
  assert.equal(selectBodyText_(false, "", true, "<p>Hello there</p>"), "Hello there");
});

test("selectBodyText_ sends an empty plain-text body as empty, never backfilled from HTML", () => {
  // A plain part that exists but happens to be empty must not be treated as
  // "missing" -- this is the specific correctness edge case the fallback
  // must not get wrong.
  assert.equal(selectBodyText_(true, "", true, "<p>should not be used</p>"), "");
});

test("selectBodyText_ returns empty when neither a plain nor an HTML part exists", () => {
  assert.equal(selectBodyText_(false, "", false, ""), "");
});

// ---- shouldTreatPermanentStreakAsSystemic_ (permanent-error circuit breaker) --

test("shouldTreatPermanentStreakAsSystemic_ is false below the threshold", () => {
  assert.equal(shouldTreatPermanentStreakAsSystemic_(1, 3), false);
  assert.equal(shouldTreatPermanentStreakAsSystemic_(2, 3), false);
});

test("shouldTreatPermanentStreakAsSystemic_ is true at and above the threshold", () => {
  assert.equal(shouldTreatPermanentStreakAsSystemic_(3, 3), true);
  assert.equal(shouldTreatPermanentStreakAsSystemic_(4, 3), true);
});

test("shouldTreatPermanentStreakAsSystemic_ falls back to the documented default threshold", () => {
  assert.equal(shouldTreatPermanentStreakAsSystemic_(PERMANENT_FAILURE_STREAK_THRESHOLD - 1, undefined), false);
  assert.equal(shouldTreatPermanentStreakAsSystemic_(PERMANENT_FAILURE_STREAK_THRESHOLD, undefined), true);
});

// ---- buildWebhookPayload_ (payload normalization) ---------------------------

function fixtureMessage(overrides) {
  return Object.assign(
    {
      id: "18d4f2b3c4a5e6f7",
      threadId: "18d4f2b3c4a5e6f0",
      internalDate: String(new Date("2026-08-13T09:30:00Z").getTime()),
      payload: {
        mimeType: "multipart/mixed",
        headers: [
          { name: "From", value: "Amos Ben-Meir <amos@example.com>" },
          { name: "To", value: "data@astronomic.com" },
          { name: "Cc", value: "chris@astronomic.com" },
          { name: "Subject", value: "Update on my info" },
        ],
        parts: [
          { mimeType: "text/plain", body: { data: "cGxhaW4tYm9keQ" } },
          { mimeType: "application/pdf", filename: "deck.pdf", body: { size: 999, attachmentId: "att1" } },
        ],
      },
    },
    overrides
  );
}

test("buildWebhookPayload_ produces the exact EmailIntakeWebhookRequest shape", () => {
  const message = fixtureMessage();
  const payload = buildWebhookPayload_(message, "plain-body-decoded", {});
  assert.deepEqual(payload, {
    gmail_message_id: "18d4f2b3c4a5e6f7",
    gmail_thread_id: "18d4f2b3c4a5e6f0",
    sender: "Amos Ben-Meir <amos@example.com>",
    recipients: ["data@astronomic.com", "chris@astronomic.com"],
    subject: "Update on my info",
    body_text: "plain-body-decoded",
    received_at: "2026-08-13T09:30:00.000Z",
    attachments: [{ filename: "deck.pdf", content_type: "application/pdf", size_bytes: 999 }],
  });
});

test("buildWebhookPayload_ uses the real Gmail message id verbatim, never a generated id", () => {
  const message = fixtureMessage({ id: "some-real-gmail-id-abc123" });
  const payload = buildWebhookPayload_(message, "", {});
  assert.equal(payload.gmail_message_id, "some-real-gmail-id-abc123");
});

test("buildWebhookPayload_ handles a message with no attachments and an empty body", () => {
  const message = fixtureMessage({
    payload: {
      mimeType: "text/plain",
      headers: [
        { name: "From", value: "a@x.com" },
        { name: "To", value: "data@astronomic.com" },
        { name: "Subject", value: "" },
      ],
      body: { data: "" },
    },
  });
  const payload = buildWebhookPayload_(message, "", {});
  assert.deepEqual(payload.attachments, []);
  assert.equal(payload.body_text, "");
  assert.equal(payload.subject, "");
});

test("buildWebhookPayload_ truncates an oversized body via the configured limit", () => {
  const message = fixtureMessage();
  const payload = buildWebhookPayload_(message, "y".repeat(20), { maxBodyChars: 5 });
  assert.match(payload.body_text, /^yyyyy\n\n\[\.\.\.truncated/);
});

test("buildWebhookPayload_ fails safely (no throw) on a message missing a From header", () => {
  const message = fixtureMessage({
    payload: { mimeType: "text/plain", headers: [{ name: "Subject", value: "no sender" }], body: { data: "" } },
  });
  const payload = buildWebhookPayload_(message, "", {});
  assert.equal(payload.sender, ""); // backend's own Field(min_length=1) rejects this as a 422 -- classified "permanent"
});

// ---- classifyHttpOutcome_ (test cases 4, 5, and the failure-handling matrix) --

test("classifyHttpOutcome_ classifies 2xx as success", () => {
  assert.equal(classifyHttpOutcome_(200), "success");
  assert.equal(classifyHttpOutcome_(201), "success");
});

test("classifyHttpOutcome_ classifies 401/403 as auth_fatal", () => {
  assert.equal(classifyHttpOutcome_(401), "auth_fatal");
  assert.equal(classifyHttpOutcome_(403), "auth_fatal");
});

test("classifyHttpOutcome_ classifies 400/404/422 as permanent", () => {
  assert.equal(classifyHttpOutcome_(400), "permanent");
  assert.equal(classifyHttpOutcome_(404), "permanent");
  assert.equal(classifyHttpOutcome_(422), "permanent");
});

test("classifyHttpOutcome_ classifies 429 and 5xx as transient", () => {
  assert.equal(classifyHttpOutcome_(429), "transient");
  assert.equal(classifyHttpOutcome_(500), "transient");
  assert.equal(classifyHttpOutcome_(502), "transient");
  assert.equal(classifyHttpOutcome_(503), "transient");
});

test("classifyHttpOutcome_ classifies a null status (network failure) as transient", () => {
  assert.equal(classifyHttpOutcome_(null), "transient");
  assert.equal(classifyHttpOutcome_(undefined), "transient");
});

test("classifyHttpOutcome_ defaults an unrecognized status to transient rather than giving up", () => {
  assert.equal(classifyHttpOutcome_(599), "transient");
  assert.equal(classifyHttpOutcome_(999), "transient");
});

// ---- DEFAULT_MAX_BODY_CHARS sanity ------------------------------------------

test("DEFAULT_MAX_BODY_CHARS is a positive, documented constant", () => {
  assert.equal(typeof DEFAULT_MAX_BODY_CHARS, "number");
  assert.ok(DEFAULT_MAX_BODY_CHARS > 0);
});
