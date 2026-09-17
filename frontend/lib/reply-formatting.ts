/**
 * Inbox V2 reply-detail page (2026-09-17) -- pure text-processing
 * helpers, no React/DOM. Both functions operate on the SAME plain-text
 * body string the backend already returns (MailInboxReplyBody.body_text,
 * already safe -- an HTML-only reply was already converted server-side,
 * see app/google/gmail_message_body_client.py). Neither function ever
 * produces or expects HTML; the page renders their output as plain text
 * nodes plus <a> elements it constructs itself, never
 * dangerouslySetInnerHTML.
 */

export interface ReplySplit {
  /** The lead's own new text, trimmed of trailing blank lines. */
  newReply: string;
  /** Everything from the quote boundary onward (the "On ... wrote:"-style
   * intro line, if one immediately precedes the quote marks, plus every
   * subsequent quoted line with its leading "> " stripped) -- null if no
   * quote marker was found at all, meaning the whole body is `newReply`
   * and nothing was split out. */
  quoted: string | null;
}

/**
 * Splits a plain-text reply into the lead's new text and any quoted
 * prior message, using ONLY the ">" quote-prefix convention Gmail's
 * plain-text export consistently uses for blockquoted content --
 * deliberately not English phrases like "wrote:" (Maximus Romy's real
 * production reply quotes a Filipino-locale "Noong ... sinulat ni ...
 * ang:" intro line, which no English-keyed heuristic would catch). This
 * is a conservative, single-signal heuristic: it only ever splits when
 * it finds an actual line starting with ">"; anything else (a reply
 * with no quote marker at all, or an HTML-converted body that never
 * produces "> " prefixes) is returned whole as `newReply` with
 * `quoted: null` rather than guessed at.
 */
export function splitReplyQuote(bodyText: string): ReplySplit {
  const lines = bodyText.split("\n");
  const firstQuoteIndex = lines.findIndex((line) => line.trimStart().startsWith(">"));

  if (firstQuoteIndex === -1) {
    return { newReply: bodyText.trim(), quoted: null };
  }

  // Walk backward past any blank lines, then backward again through the
  // whole non-blank paragraph immediately before the first "> " line --
  // that paragraph is almost always the locale-specific "on X wrote:"
  // intro (English, Filipino, whatever the sender's Gmail locale is),
  // which Gmail sometimes wraps across more than one physical line with
  // no blank line of its own. Fold the whole paragraph into the quoted
  // section rather than leaving part of it dangling at the end of
  // newReply. Bounded by the nearest blank line (or the start of the
  // text) either way, so this never reaches back further than one
  // paragraph -- still a conservative, bounded heuristic.
  let quoteStart = firstQuoteIndex;
  let i = firstQuoteIndex - 1;
  while (i >= 0 && lines[i].trim().length === 0) {
    i--;
  }
  if (i >= 0 && !lines[i].trimStart().startsWith(">")) {
    let paragraphStart = i;
    while (
      paragraphStart - 1 >= 0 &&
      lines[paragraphStart - 1].trim().length > 0 &&
      !lines[paragraphStart - 1].trimStart().startsWith(">")
    ) {
      paragraphStart--;
    }
    quoteStart = paragraphStart;
  }

  const newReply = lines
    .slice(0, quoteStart)
    .join("\n")
    .replace(/\n+$/, "")
    .trim();

  const quotedLines = lines.slice(quoteStart).map((line) => {
    const trimmed = line.trimStart();
    if (trimmed.startsWith(">")) {
      return trimmed.replace(/^>+\s?/, "");
    }
    return line;
  });
  const quoted = quotedLines.join("\n").trim();

  return { newReply, quoted: quoted.length > 0 ? quoted : null };
}

const URL_PATTERN = /\bhttps?:\/\/[^\s<>()[\]"']+[^\s<>()[\].,!?"':;]/g;

export type TextSegment = { type: "text"; value: string } | { type: "link"; href: string; label: string };

/**
 * Segments plain text into alternating text/link parts for the caller
 * to render as real React nodes (never dangerouslySetInnerHTML). Only
 * ever recognizes http(s):// URLs -- deliberately no bare-domain
 * guessing (e.g. "example.com" without a scheme), which risks false
 * positives inside ordinary prose far more than it helps. Trailing
 * punctuation immediately after a URL (a period ending a sentence, a
 * closing paren from surrounding prose) is excluded from the link
 * itself.
 */
export function linkifySegments(text: string): TextSegment[] {
  const segments: TextSegment[] = [];
  let lastIndex = 0;
  for (const match of text.matchAll(URL_PATTERN)) {
    const start = match.index ?? 0;
    if (start > lastIndex) {
      segments.push({ type: "text", value: text.slice(lastIndex, start) });
    }
    segments.push({ type: "link", href: match[0], label: match[0] });
    lastIndex = start + match[0].length;
  }
  if (lastIndex < text.length) {
    segments.push({ type: "text", value: text.slice(lastIndex) });
  }
  if (segments.length === 0) {
    return [{ type: "text", value: text }];
  }
  return segments;
}
