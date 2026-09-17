import { test } from "node:test";
import assert from "node:assert/strict";
import { linkifySegments, splitReplyQuote } from "./reply-formatting.ts";

// Fixtures are the ACTUAL production bodies returned for the pilot's two
// real replies (verified 2026-09-17 against live Gmail) -- not invented
// text -- so the quote-split heuristic is proven against the real,
// messy shape it has to handle, including a non-English quote intro.

const JOHN_BODY =
  'Nice to hear from you. this is great news.\r\nJohn\r\n\r\nOn Thu, Sep 17, 2026 at 12:47 PM <victoria@useastronomic.com> wrote:\r\n\r\n> Hi John Adrian,\r\n>\r\n> Quick note from Astronomic — we\'re testing a new way of managing our\r\n> outreach and wanted to include a small group of people we already know in\r\n> the first live run.\r\n>\r\n> No action needed, but if you see this, feel free to reply with a quick\r\n> "got it." It will help us confirm everything is working properly on our\r\n> side.\r\n>\r\n> Best,\r\n> Victoria\r\n> ------------------------------\r\n>\r\n> Don\'t want these emails? Unsubscribe\r\n> <https://api.astronomicconnect.com/mail/unsubscribe?token=abc123>\r\n>\r\n'.replace(
    /\r\n/g,
    "\n"
  );

const MAXIMUS_BODY =
  "nice to meet you.\n\n- Maximus\n\nNoong Huw, Set 17, 2026 nang 12:45 PM, sinulat ni <\nvictoria@useastronomic.com> ang:\n\n> Hi Maximus,\n>\n> Quick note from Astronomic — we're testing a new way of managing our\n> outreach and wanted to include a small group of people we already know in\n> the first live run.\n>\n> Best,\n> Victoria\n>\n";

test("splitReplyQuote separates John's real reply from Gmail's English quote header", () => {
  const { newReply, quoted } = splitReplyQuote(JOHN_BODY);
  assert.equal(newReply, "Nice to hear from you. this is great news.\nJohn");
  assert.ok(quoted);
  assert.match(quoted!, /^On Thu, Sep 17, 2026 at 12:47 PM/);
  assert.match(quoted!, /Hi John Adrian,/);
  assert.doesNotMatch(quoted!, /^>/m); // leading "> " markers stripped
});

test("splitReplyQuote separates Maximus's real reply from a non-English (Filipino) quote header", () => {
  // The whole point of using only the ">" signal, not an English phrase
  // match -- this is real production data with a Filipino Gmail locale.
  const { newReply, quoted } = splitReplyQuote(MAXIMUS_BODY);
  assert.equal(newReply, "nice to meet you.\n\n- Maximus");
  assert.ok(quoted);
  assert.match(quoted!, /Noong Huw, Set 17, 2026/);
  assert.match(quoted!, /Hi Maximus,/);
});

test("splitReplyQuote returns the whole body as newReply, quoted null, when there is no '>' marker at all", () => {
  const result = splitReplyQuote("Just a short reply with nothing quoted.");
  assert.equal(result.newReply, "Just a short reply with nothing quoted.");
  assert.equal(result.quoted, null);
});

test("splitReplyQuote never fabricates a split for an empty or whitespace-only body", () => {
  assert.deepEqual(splitReplyQuote(""), { newReply: "", quoted: null });
  assert.deepEqual(splitReplyQuote("   \n  "), { newReply: "", quoted: null });
});

test("splitReplyQuote handles a body that is quote lines from the very first line (no intro, no new text)", () => {
  const result = splitReplyQuote("> Hi there,\n> quoted only");
  assert.equal(result.newReply, "");
  assert.ok(result.quoted);
  assert.match(result.quoted!, /^Hi there,/);
});

test("splitReplyQuote is conservative: a reply that merely CONTAINS a literal '>' mid-sentence, not as a line prefix, is not split", () => {
  const result = splitReplyQuote("Revenue grew 5 -> 10 this quarter, thanks!");
  assert.equal(result.quoted, null);
  assert.equal(result.newReply, "Revenue grew 5 -> 10 this quarter, thanks!");
});

test("linkifySegments turns a bare http(s) URL into its own link segment", () => {
  const segments = linkifySegments("See https://example.com/path for details.");
  assert.deepEqual(segments, [
    { type: "text", value: "See " },
    { type: "link", href: "https://example.com/path", label: "https://example.com/path" },
    { type: "text", value: " for details." },
  ]);
});

test("linkifySegments excludes trailing sentence punctuation from the link itself", () => {
  const segments = linkifySegments("Visit https://example.com/page.");
  const link = segments.find((s) => s.type === "link");
  assert.equal(link?.href, "https://example.com/page");
});

test("linkifySegments handles the real unsubscribe URL from John's quoted footer", () => {
  const segments = linkifySegments("<https://api.astronomicconnect.com/mail/unsubscribe?token=abc123>");
  const link = segments.find((s) => s.type === "link");
  assert.equal(link?.href, "https://api.astronomicconnect.com/mail/unsubscribe?token=abc123");
});

test("linkifySegments returns a single text segment for plain text with no URLs", () => {
  assert.deepEqual(linkifySegments("nice to meet you."), [{ type: "text", value: "nice to meet you." }]);
});

test("linkifySegments handles multiple URLs in the same text", () => {
  const segments = linkifySegments("First https://a.example then https://b.example done.");
  const links = segments.filter((s) => s.type === "link").map((s) => (s as { href: string }).href);
  assert.deepEqual(links, ["https://a.example", "https://b.example"]);
});

test("linkifySegments never invents a link for plain domain-looking text without a scheme", () => {
  const segments = linkifySegments("Reach us at astronomic.com or example.org anytime.");
  assert.equal(segments.some((s) => s.type === "link"), false);
});
