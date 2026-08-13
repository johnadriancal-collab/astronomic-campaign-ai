/**
 * Email Intake Phase 2A -- pure transport logic.
 *
 * Every function here is plain ECMAScript with NO dependency on any
 * Apps Script global (GmailApp, Gmail, PropertiesService, UrlFetchApp,
 * Utilities, LockService, Logger). That is deliberate: Apps Script has no
 * module system, so this file is pasted into the Apps Script project
 * as a second script file (Code.gs calls these functions directly, as
 * plain globals -- Apps Script concatenates every file in a project into
 * one shared execution context, no import/require needed there).
 *
 * The SAME file is required unmodified by logic.test.js under plain
 * Node (`node --test apps_script/email_intake/logic.test.js`), which is
 * what makes this logic testable without a live Gmail account. The
 * module.exports guard at the bottom is a no-op inside Apps Script
 * (`module` is undefined there).
 *
 * Code.gs owns everything that actually touches Gmail/HTTP/secrets:
 * base64url body decoding (via Utilities, which already does this
 * correctly for UTF-8 -- reimplementing it here would just be a second,
 * untested decoder), the HTTP call itself, label reads/writes, and
 * Script Properties. This file only decides WHAT to do with data it's
 * handed, never how to fetch it.
 */

// ---- Activation timestamp --------------------------------------------------

/**
 * Parses the EMAIL_INTAKE_ACTIVATION_DATE script property into epoch
 * milliseconds. Requires a FULL timestamp (date + time + offset/zone),
 * not a bare calendar date -- this is the hard safety boundary the whole
 * transport depends on, so an ambiguous or unparseable value is a
 * configuration error, not something to silently default around.
 * Throws Error with a message safe to log (never includes secrets).
 */
function parseActivationInstant_(rawValue) {
  if (!rawValue || typeof rawValue !== "string" || !rawValue.trim()) {
    throw new Error(
      "EMAIL_INTAKE_ACTIVATION_DATE is not set. Refusing to run -- without an " +
        "activation timestamp there is no safe boundary against ingesting historical mail."
    );
  }
  var trimmed = rawValue.trim();
  // Require an explicit time-of-day component -- "2026-08-13" alone parses
  // in JS as UTC midnight, which is a real timestamp but almost certainly NOT
  // what an operator meant when they typed a bare date; the activation
  // instructions explicitly call for a full timestamp with offset/zone.
  if (!/T\d{2}:\d{2}/.test(trimmed)) {
    throw new Error(
      'EMAIL_INTAKE_ACTIVATION_DATE ("' +
        trimmed +
        '") does not look like a full timestamp (expected e.g. ' +
        '"2026-08-13T00:00:00+08:00"). A bare calendar date is not accepted.'
    );
  }
  var parsed = new Date(trimmed);
  if (isNaN(parsed.getTime())) {
    throw new Error('EMAIL_INTAKE_ACTIVATION_DATE ("' + trimmed + '") could not be parsed as a timestamp.');
  }
  return parsed.getTime();
}

/**
 * Coarse day-level lower bound for Gmail's `after:` search operator, which
 * only understands whole calendar days (in the Gmail account's own time
 * zone) -- it cannot express a time-of-day boundary. This is used ONLY to
 * shrink the candidate scan window for performance; it is deliberately one
 * full day EARLIER than the real activation instant so it can never
 * exclude a message that isMessageEligibleByTime_ would consider eligible.
 * The exact boundary is always re-checked per-message in code -- this
 * value must never be treated as the safety boundary itself.
 */
function coarseSearchFloorDate_(activationInstantMs) {
  var oneDayMs = 24 * 60 * 60 * 1000;
  var floor = new Date(activationInstantMs - oneDayMs);
  var y = floor.getUTCFullYear();
  var m = String(floor.getUTCMonth() + 1).padStart(2, "0");
  var d = String(floor.getUTCDate()).padStart(2, "0");
  return y + "/" + m + "/" + d;
}

/**
 * The one safety check that actually matters: is this specific message's
 * own Gmail-assigned receipt time at or after activation? `internalDateMs`
 * should come from the message's `internalDate` field (Gmail's own
 * server-assigned receipt timestamp), never the sender-controlled `Date:`
 * header, which can be missing, wrong, or spoofed.
 */
function isMessageEligibleByTime_(internalDateMs, activationInstantMs) {
  return typeof internalDateMs === "number" && !isNaN(internalDateMs) && internalDateMs >= activationInstantMs;
}

/**
 * Builds the Gmail search query string. `after:` is a coarse, one-day-
 * early pre-filter only (see coarseSearchFloorDate_ above) -- the real
 * activation boundary is enforced per-message via isMessageEligibleByTime_,
 * never by this query alone. Excluding both labels keeps a permanently-
 * failed message (see Code.gs's ERROR_LABEL_NAME) out of the candidate
 * list too, so it stops being re-fetched every run once flagged.
 */
function buildSearchQuery_(activationInstantMs, processedLabelName, errorLabelName) {
  var floorDate = coarseSearchFloorDate_(activationInstantMs);
  return "-label:" + processedLabelName + " -label:" + errorLabelName + " after:" + floorDate;
}

/**
 * Defense-in-depth check used alongside the search query above: even if
 * Gmail's search index is momentarily stale relative to a just-applied
 * label, a message actually carrying either label (checked directly on
 * the message's own labelIds) is treated as already handled and skipped.
 */
function hasProcessedOrErrorLabel_(labelIds, processedLabelId, errorLabelId) {
  var ids = labelIds || [];
  return ids.indexOf(processedLabelId) !== -1 || ids.indexOf(errorLabelId) !== -1;
}

// ---- Header parsing ---------------------------------------------------------

/** Case-insensitive lookup in a Gmail API headers array: [{name, value}, ...]. */
function findHeader_(headers, name) {
  if (!headers) return "";
  var lower = name.toLowerCase();
  for (var i = 0; i < headers.length; i++) {
    if (headers[i] && typeof headers[i].name === "string" && headers[i].name.toLowerCase() === lower) {
      return headers[i].value || "";
    }
  }
  return "";
}

/**
 * Splits a header value like `"Doe, Jane" <jane@x.com>, bob@y.com` into
 * individual address entries, respecting double-quoted display names so a
 * comma INSIDE a quoted name is not mistaken for an address separator.
 * Never invents, drops a display name, or infers anything -- purely a
 * delimiter fix-up on what the header already says.
 */
function splitAddressList_(headerValue) {
  if (!headerValue || typeof headerValue !== "string") return [];
  var entries = [];
  var current = "";
  var inQuotes = false;
  for (var i = 0; i < headerValue.length; i++) {
    var ch = headerValue[i];
    if (ch === '"') {
      inQuotes = !inQuotes;
      current += ch;
    } else if (ch === "," && !inQuotes) {
      entries.push(current.trim());
      current = "";
    } else {
      current += ch;
    }
  }
  if (current.trim()) entries.push(current.trim());
  return entries.filter(function (e) {
    return e.length > 0;
  });
}

// ---- MIME tree walking (payload is the Gmail API's own JSON shape) --------

/**
 * Depth-first search for the first part whose mimeType matches exactly.
 * Returns { mimeType, data } where `data` is the RAW base64url string as
 * Gmail returns it (never decoded here -- see this file's header comment
 * for why decoding lives in Code.gs). Returns null if no such part exists.
 * Handles both a non-multipart message (payload itself has mimeType+body)
 * and an arbitrarily nested multipart/* structure.
 */
function findBodyPart_(payload, mimeType) {
  if (!payload) return null;
  if (payload.mimeType === mimeType && payload.body && payload.body.data) {
    return { mimeType: payload.mimeType, data: payload.body.data };
  }
  if (payload.parts && payload.parts.length) {
    for (var i = 0; i < payload.parts.length; i++) {
      var found = findBodyPart_(payload.parts[i], mimeType);
      if (found) return found;
    }
  }
  return null;
}

/**
 * Collects attachment METADATA ONLY from a message's MIME tree: filename,
 * content type, and size in bytes -- every value the Gmail API already
 * reports on the part itself, with no separate attachment-content fetch.
 * A part counts as an attachment when it has a non-empty `filename`,
 * matching how Gmail itself distinguishes a real attachment from an
 * inline/text body part. This function must NEVER be extended to fetch
 * `body.attachmentId` content -- Phase 1 (and this phase) only ever
 * support attachment metadata, by design.
 */
function findAttachmentParts_(payload) {
  var results = [];
  function walk(part) {
    if (!part) return;
    if (part.filename && part.filename.length > 0) {
      results.push({
        filename: part.filename,
        content_type: part.mimeType || null,
        size_bytes: part.body && typeof part.body.size === "number" ? part.body.size : null,
      });
    }
    if (part.parts && part.parts.length) {
      for (var i = 0; i < part.parts.length; i++) walk(part.parts[i]);
    }
  }
  walk(payload);
  return results;
}

// ---- Body length safety net -------------------------------------------------

/** Documented, non-silent default cap -- see this file's header + SETUP.md. */
var DEFAULT_MAX_BODY_CHARS = 100000;

/**
 * Truncates only if genuinely over the limit, and always appends a visible
 * marker recording the ORIGINAL length -- a reviewer must never mistake a
 * truncated body for the complete message. maxChars <= 0 or non-numeric
 * falls back to DEFAULT_MAX_BODY_CHARS rather than disabling the cap.
 */
function truncateBodyIfNeeded_(text, maxChars) {
  var limit = typeof maxChars === "number" && maxChars > 0 ? maxChars : DEFAULT_MAX_BODY_CHARS;
  if (!text || text.length <= limit) return text || "";
  return (
    text.slice(0, limit) +
    "\n\n[...truncated by the Email Intake Apps Script transport -- original length: " +
    text.length +
    " characters, limit: " +
    limit +
    "...]"
  );
}

// ---- HTML fallback (when no text/plain part exists) ------------------------

/**
 * Mechanical, deterministic HTML -> plain text fallback -- NOT extraction,
 * NOT AI/LLM interpretation, just tag/entity stripping. Used only when a
 * message has no text/plain part at all (a normal, common case -- HTML-only
 * composition is not malformed). Strips <script>/<style> content entirely,
 * converts a small set of block-level boundaries to line breaks so text
 * doesn't get mashed together, strips remaining tags, decodes a small FIXED
 * set of common named entities plus numeric entities (anything unrecognized
 * is left as-is rather than guessed at), and collapses excess whitespace.
 */
function stripHtmlToPlainText_(html) {
  if (!html || typeof html !== "string") return "";
  var text = html;
  text = text.replace(/<script[\s\S]*?<\/script>/gi, " ");
  text = text.replace(/<style[\s\S]*?<\/style>/gi, " ");
  text = text.replace(/<(br|\/p|\/div|\/tr|\/li|\/h[1-6])\s*\/?>/gi, "\n");
  text = text.replace(/<[^>]*>/g, "");

  var NAMED_ENTITIES = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " " };
  text = text.replace(/&(#\d+|#x[0-9a-fA-F]+|[a-zA-Z]+);/g, function (match, code) {
    if (code.charAt(0) === "#") {
      var isHex = code.charAt(1) === "x" || code.charAt(1) === "X";
      var codePoint = isHex ? parseInt(code.slice(2), 16) : parseInt(code.slice(1), 10);
      return isNaN(codePoint) ? match : String.fromCharCode(codePoint);
    }
    return Object.prototype.hasOwnProperty.call(NAMED_ENTITIES, code) ? NAMED_ENTITIES[code] : match;
  });

  text = text.replace(/[ \t]+/g, " ");
  text = text.replace(/\n{3,}/g, "\n\n");
  return text.trim();
}

/**
 * Decides what body text to send, given whether a text/plain part was
 * found at all (not just whether its decoded text happens to be a non-
 * empty string -- an EMPTY plain-text body must not fall back to HTML).
 * Falls back to a stripped text/html part only when no plain part exists;
 * falls back to "" only when neither exists.
 */
function selectBodyText_(plainPartFound, decodedPlainText, htmlPartFound, decodedHtmlText) {
  if (plainPartFound) return decodedPlainText || "";
  if (htmlPartFound) return stripHtmlToPlainText_(decodedHtmlText || "");
  return "";
}

// ---- Permanent-failure circuit breaker --------------------------------------

/**
 * A run-level circuit breaker, NOT a per-message rule. 400/404/422 are
 * classified "permanent" per-message (see classifyHttpOutcome_), but a
 * STREAK of consecutive permanent failures across DIFFERENT messages in
 * the same run is much more likely evidence of a systemic problem (wrong
 * webhook URL, a temporary backend contract regression, a deploy mismatch
 * returning 404 for everyone) than of that many independent messages each
 * happening to be individually invalid. Once the streak reaches the
 * threshold, the caller (Code.gs) stops attempting further candidates in
 * this run rather than continuing to permanently label more messages that
 * were most likely never actually message-specific problems.
 */
var PERMANENT_FAILURE_STREAK_THRESHOLD = 3;

function shouldTreatPermanentStreakAsSystemic_(consecutivePermanentCount, threshold) {
  var limit = typeof threshold === "number" && threshold > 0 ? threshold : PERMANENT_FAILURE_STREAK_THRESHOLD;
  return consecutivePermanentCount >= limit;
}

// ---- Webhook payload construction ------------------------------------------

/**
 * Builds the exact JSON body POST /sync/email-intake expects
 * (app/models/email_intake.py: EmailIntakeWebhookRequest), from a Gmail
 * API message object and its ALREADY-DECODED plain-text body (decoding
 * happens in Code.gs -- see this file's header comment). Pure and
 * side-effect-free: never calls Gmail, HTTP, or logging.
 *
 * `message` is the plain-JSON shape the Gmail advanced API's
 * Users.messages.get returns: { id, threadId, internalDate, payload:
 * { headers: [...], parts: [...] } }.
 */
function buildWebhookPayload_(message, decodedBodyText, options) {
  var opts = options || {};
  var headers = (message.payload && message.payload.headers) || [];
  var sender = findHeader_(headers, "From");
  var to = splitAddressList_(findHeader_(headers, "To"));
  var cc = splitAddressList_(findHeader_(headers, "Cc"));
  var subject = findHeader_(headers, "Subject");
  var attachments = findAttachmentParts_(message.payload);
  var internalDateMs = parseInt(message.internalDate, 10);

  return {
    gmail_message_id: message.id,
    gmail_thread_id: message.threadId || null,
    sender: sender,
    recipients: to.concat(cc),
    subject: subject || "",
    body_text: truncateBodyIfNeeded_(decodedBodyText || "", opts.maxBodyChars),
    received_at: new Date(internalDateMs).toISOString(),
    attachments: attachments,
  };
}

// ---- Webhook response classification ---------------------------------------

/**
 * Buckets an HTTP outcome for the retry policy documented in Code.gs:
 *   "success"    2xx                          -> apply crm-intake-processed
 *   "auth_fatal" 401/403                      -> abort the WHOLE run now
 *   "permanent"  400/404/422                  -> apply crm-intake-error, do not retry automatically
 *   "transient"  429/5xx/network/anything else -> leave untouched, retry next run
 * `statusCode` may be null (network-level failure, no response at all),
 * which is treated as transient -- the message stays eligible.
 */
function classifyHttpOutcome_(statusCode) {
  if (statusCode === null || typeof statusCode === "undefined") return "transient";
  if (statusCode >= 200 && statusCode < 300) return "success";
  if (statusCode === 401 || statusCode === 403) return "auth_fatal";
  if (statusCode === 400 || statusCode === 404 || statusCode === 422) return "permanent";
  // Includes 429 and all 5xx explicitly, and anything unrecognized -- safer
  // to retry an outcome we don't understand than to silently give up on it.
  return "transient";
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    DEFAULT_MAX_BODY_CHARS: DEFAULT_MAX_BODY_CHARS,
    PERMANENT_FAILURE_STREAK_THRESHOLD: PERMANENT_FAILURE_STREAK_THRESHOLD,
    parseActivationInstant_: parseActivationInstant_,
    coarseSearchFloorDate_: coarseSearchFloorDate_,
    isMessageEligibleByTime_: isMessageEligibleByTime_,
    buildSearchQuery_: buildSearchQuery_,
    hasProcessedOrErrorLabel_: hasProcessedOrErrorLabel_,
    findHeader_: findHeader_,
    splitAddressList_: splitAddressList_,
    findBodyPart_: findBodyPart_,
    findAttachmentParts_: findAttachmentParts_,
    truncateBodyIfNeeded_: truncateBodyIfNeeded_,
    stripHtmlToPlainText_: stripHtmlToPlainText_,
    selectBodyText_: selectBodyText_,
    shouldTreatPermanentStreakAsSystemic_: shouldTreatPermanentStreakAsSystemic_,
    buildWebhookPayload_: buildWebhookPayload_,
    classifyHttpOutcome_: classifyHttpOutcome_,
  };
}
