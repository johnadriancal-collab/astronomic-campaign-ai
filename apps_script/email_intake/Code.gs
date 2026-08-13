/**
 * Email -> CRM Intake, Phase 2A: Gmail transport bridge.
 *
 * A standalone Apps Script project (not bound to a Sheet/Form -- see
 * SETUP.md for why) that periodically scans data@astronomic.com for
 * eligible messages and forwards each one, individually, to the existing
 * Phase 1 webhook (POST /sync/email-intake). This script does exactly
 * three things and nothing else:
 *
 *   1. Find candidate Gmail messages received on/after a configured
 *      activation timestamp that do not yet carry the crm-intake-processed
 *      (or crm-intake-error) label.
 *   2. POST each one, unmodified beyond MIME-decoding its own plain-text
 *      body, to the webhook that already enforces every Phase 1 safety
 *      rule (human approval required before any CRM write).
 *   3. Label the message crm-intake-processed ONLY after the webhook
 *      confirms it accepted the message.
 *
 * It contains NO extraction, NO matching, NO CRM write of any kind, and
 * NO AI/Claude call -- all of that already lives behind the webhook and
 * is intentionally out of scope here. See logic.js for the pure decision
 * functions this file calls into (activation-timestamp math, header
 * parsing, MIME walking, payload shape, response classification) --
 * logic.js has no Apps Script dependency and is unit-tested under plain
 * Node; this file is the thin Gmail/HTTP glue around it.
 *
 * IMPORTANT -- per-message labels require the ADVANCED Gmail API, not the
 * classic GmailApp service. Gmail labels are fundamentally THREAD-scoped
 * in the classic GmailApp API (GmailThread.addLabel(), no such method on
 * GmailMessage) -- that is unsafe here: if an active thread gets label A
 * applied after its first message is processed, a genuinely NEW message
 * arriving later in that same thread would then be wrongly hidden by a
 * `-label:crm-intake-processed` search, since the whole thread already
 * carries the label. The advanced `Gmail` service (Users.messages.list /
 * .get / .modify) applies and queries labels per INDIVIDUAL message,
 * which is what makes "treat each message as its own ingestion unit,
 * forever, even in an active thread" actually hold. See SETUP.md step 2
 * for the one extra "enable a service" click this requires.
 */

// ---- Configuration (read from Script Properties, never hardcoded) --------

function getConfig_() {
  var props = PropertiesService.getScriptProperties();
  var webhookUrl = props.getProperty("EMAIL_INTAKE_WEBHOOK_URL");
  var webhookToken = props.getProperty("EMAIL_INTAKE_WEBHOOK_TOKEN");
  var activationDateRaw = props.getProperty("EMAIL_INTAKE_ACTIVATION_DATE");
  var environmentLabel = props.getProperty("EMAIL_INTAKE_ENVIRONMENT_LABEL") || "(unset)";
  var maxMessagesPerRun = parseInt(props.getProperty("EMAIL_INTAKE_MAX_MESSAGES_PER_RUN"), 10);
  var maxBodyChars = parseInt(props.getProperty("EMAIL_INTAKE_MAX_BODY_CHARS"), 10);

  // Fail closed: any missing required value stops the run before it ever
  // touches Gmail. This function throws (never returns a partial config)
  // on purpose -- see logic.js:parseActivationInstant_ for the same rule
  // applied to the activation timestamp's exact format.
  if (!webhookUrl) throw new Error("EMAIL_INTAKE_WEBHOOK_URL is not set in Script Properties.");
  if (!webhookToken) throw new Error("EMAIL_INTAKE_WEBHOOK_TOKEN is not set in Script Properties.");
  var activationInstantMs = parseActivationInstant_(activationDateRaw); // throws if missing/invalid

  return {
    webhookUrl: webhookUrl,
    webhookToken: webhookToken,
    activationInstantMs: activationInstantMs,
    environmentLabel: environmentLabel,
    maxMessagesPerRun: !isNaN(maxMessagesPerRun) && maxMessagesPerRun > 0 ? maxMessagesPerRun : 20,
    maxBodyChars: !isNaN(maxBodyChars) && maxBodyChars > 0 ? maxBodyChars : DEFAULT_MAX_BODY_CHARS,
  };
}

/**
 * Advisory only -- logs a warning if the URL and the operator-set
 * environment label look mismatched. Never blocks a run: a heuristic this
 * simple (string sniffing a URL) is too easy to get wrong to trust as a
 * hard gate, but a loud log line costs nothing and catches the easy
 * mistake ("script still points at localhost" / "environment label says
 * local but the URL is the real Railway domain").
 */
function warnIfEnvironmentLooksMismatched_(config) {
  var urlLooksLocal = /localhost|127\.0\.0\.1/i.test(config.webhookUrl);
  var labelLower = config.environmentLabel.toLowerCase();
  var labelSaysProd = labelLower.indexOf("prod") !== -1;
  var labelSaysLocal = labelLower.indexOf("local") !== -1 || labelLower.indexOf("dev") !== -1;
  if (urlLooksLocal && labelSaysProd) {
    Logger.log(
      "WARNING: EMAIL_INTAKE_ENVIRONMENT_LABEL says '" +
        config.environmentLabel +
        "' but EMAIL_INTAKE_WEBHOOK_URL looks local. Double-check before relying on this run."
    );
  } else if (!urlLooksLocal && labelSaysLocal) {
    Logger.log(
      "WARNING: EMAIL_INTAKE_ENVIRONMENT_LABEL says '" +
        config.environmentLabel +
        "' but EMAIL_INTAKE_WEBHOOK_URL does not look local. Double-check this script isn't pointed at production by mistake."
    );
  }
}

// ---- Label management (advanced Gmail API, per-message) -------------------

var PROCESSED_LABEL_NAME = "crm-intake-processed";
// Applied only to a message whose webhook submission was PERMANENTLY
// rejected (400/404/422) -- see classifyHttpOutcome_ in logic.js and the
// run loop below. This intentionally stops that one message from being
// retried on every future run (avoiding endless futile calls and log
// noise for something that will never succeed without operator action),
// at the cost of requiring a human to notice it and remove the label by
// hand in Gmail to re-queue it after a real fix. That tradeoff -- visible,
// bounded failure vs. silent infinite retries -- is why this label exists
// at all; it is never treated as an idempotency key by any code here.
var ERROR_LABEL_NAME = "crm-intake-error";

function getOrCreateLabelId_(name) {
  var list = Gmail.Users.Labels.list("me");
  var labels = list.labels || [];
  for (var i = 0; i < labels.length; i++) {
    if (labels[i].name === name) return labels[i].id;
  }
  var created = Gmail.Users.Labels.create({ name: name, labelListVisibility: "labelShow", messageListVisibility: "show" }, "me");
  return created.id;
}

function applyLabelToMessage_(messageId, labelId) {
  Gmail.Users.Messages.modify({ addLabelIds: [labelId] }, "me", messageId);
}

// ---- Candidate search --------------------------------------------------

// buildSearchQuery_ and hasProcessedOrErrorLabel_ live in logic.js (pure,
// unit-tested under Node) -- called directly below as plain globals, same
// as every other logic.js function in this file.

// ---- Body decoding (the one step that legitimately needs Utilities) ------

/**
 * Decodes a Gmail API base64url body part into a UTF-8 string using
 * Utilities -- Google's own, already-correct implementation. Deliberately
 * NOT reimplemented in logic.js (see that file's header comment for why).
 */
function decodeBase64UrlToUtf8_(base64UrlData) {
  var bytes = Utilities.base64DecodeWebSafe(base64UrlData);
  return Utilities.newBlob(bytes).getDataAsString("UTF-8");
}

// ---- HTTP -------------------------------------------------------------

/**
 * POSTs once to the Phase 1 webhook. No retry loop here -- retrying is
 * handled naturally by leaving the message unlabeled and eligible for the
 * NEXT scheduled run, per the design in SETUP.md/the Phase 2A report
 * (a tight in-run retry loop would just burn Apps Script's 6-minute
 * execution budget on a backend outage that a fixed retry count can't
 * fix anyway). `muteHttpExceptions: true` so a non-2xx is inspectable
 * rather than thrown. The Authorization header is never logged anywhere
 * in this file, and this function's return value never includes it.
 */
function postToEmailIntakeWebhook_(config, payload) {
  var options = {
    method: "post",
    contentType: "application/json",
    headers: { Authorization: "Bearer " + config.webhookToken },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true,
  };
  try {
    var response = UrlFetchApp.fetch(config.webhookUrl, options);
    var code = response.getResponseCode();
    return { code: code, body: safeParseJson_(response.getContentText()) };
  } catch (fetchErr) {
    // Network-level failure (DNS, timeout, connection refused, etc.) --
    // no response at all. Classified identically to a 5xx: transient.
    return { code: null, body: null, error: String(fetchErr) };
  }
}

function safeParseJson_(text) {
  try {
    return JSON.parse(text);
  } catch (e) {
    return { raw: text };
  }
}

// ---- Main run loop ----------------------------------------------------

/**
 * Trigger entry point AND the function to run manually for the one-message
 * launch-checklist test (see SETUP.md). Safe to run as often as you like --
 * LockService prevents two overlapping executions, and even without the
 * lock, the backend's own gmail_message_id idempotency (see EmailIntake-
 * Service.ingest()) makes a genuinely-overlapping duplicate call harmless:
 * the second call just gets back `already_processed: true` for the same
 * intake item, and this script then safely (re-)applies the processed
 * label. No CRM contact is ever created or modified by anything in this
 * file, at any point, under any outcome.
 */
function runEmailIntakeSync() {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) {
    Logger.log("runEmailIntakeSync: another run already holds the lock -- skipping this execution entirely.");
    return;
  }

  var counts = {
    candidates: 0,
    skippedBeforeActivation: 0,
    skippedAlreadyLabeled: 0,
    submittedNew: 0,
    submittedAlreadyProcessed: 0,
    permanentFailures: 0,
    transientFailures: 0,
    authFatal: false,
    consecutivePermanentFailures: 0,
    systemicPermanentSuspected: false,
  };

  try {
    var config = getConfig_(); // throws -> fail closed, nothing below runs
    warnIfEnvironmentLooksMismatched_(config);
    Logger.log(
      "runEmailIntakeSync starting -- environment=" +
        config.environmentLabel +
        " activation=" +
        new Date(config.activationInstantMs).toISOString() +
        " maxMessagesPerRun=" +
        config.maxMessagesPerRun
    );

    var processedLabelId = getOrCreateLabelId_(PROCESSED_LABEL_NAME);
    var errorLabelId = getOrCreateLabelId_(ERROR_LABEL_NAME);

    var query = buildSearchQuery_(config.activationInstantMs, PROCESSED_LABEL_NAME, ERROR_LABEL_NAME);
    var listResp = Gmail.Users.Messages.list("me", { q: query, maxResults: config.maxMessagesPerRun });
    var candidates = listResp.messages || [];
    counts.candidates = candidates.length;

    for (var i = 0; i < candidates.length; i++) {
      processOneCandidate_(config, candidates[i].id, processedLabelId, errorLabelId, counts);
      if (counts.authFatal) break; // systemic auth failure -- stop the whole run, see below
      if (counts.systemicPermanentSuspected) break; // streak of permanent failures -- likely systemic, see below
    }
  } catch (configErr) {
    Logger.log("runEmailIntakeSync ABORTED before any Gmail access: " + configErr);
    return;
  } finally {
    lock.releaseLock();
  }

  Logger.log(
    "runEmailIntakeSync finished -- candidates=" +
      counts.candidates +
      " submittedNew=" +
      counts.submittedNew +
      " submittedAlreadyProcessed=" +
      counts.submittedAlreadyProcessed +
      " skippedBeforeActivation=" +
      counts.skippedBeforeActivation +
      " skippedAlreadyLabeled=" +
      counts.skippedAlreadyLabeled +
      " permanentFailures=" +
      counts.permanentFailures +
      " transientFailures=" +
      counts.transientFailures +
      (counts.authFatal ? " AUTH_FATAL=true (run stopped early -- check EMAIL_INTAKE_WEBHOOK_TOKEN)" : "") +
      (counts.systemicPermanentSuspected
        ? " SYSTEMIC_PERMANENT_FAILURE_SUSPECTED=true (run stopped early after " +
          PERMANENT_FAILURE_STREAK_THRESHOLD +
          " consecutive 400/404/422 responses -- check EMAIL_INTAKE_WEBHOOK_URL and the backend deploy/contract before assuming these messages are individually bad)"
        : "")
  );
}

/**
 * Handles exactly one candidate message id. Never throws -- every failure
 * mode is caught and counted so one bad message can never abort the loop.
 * Two cases deliberately DO stop the whole run rather than just this one
 * message, because they are evidence of a SYSTEMIC problem, not a bad
 * individual message: counts.authFatal (401/403 -- see classifyHttpOutcome_
 * in logic.js) and counts.systemicPermanentSuspected (a streak of
 * consecutive 400/404/422 responses -- see shouldTreatPermanentStreakAsSystemic_
 * in logic.js and this function's own comments below).
 *
 * Three independent failure domains are separated on purpose, each with
 * its own try/catch, so a failure in one is never misclassified as another:
 *   1. Fetching the message from Gmail (transient -- network/quota/API).
 *   2. Parsing this message's OWN content into a valid payload (permanent --
 *      a data/format problem with this specific message; retrying will hit
 *      the exact same error every time, so it gets the same "flag and stop
 *      auto-retrying" treatment as a 400/404/422 webhook rejection).
 *   3. The webhook call itself (classified by classifyHttpOutcome_).
 */
function processOneCandidate_(config, messageId, processedLabelId, errorLabelId, counts) {
  var message;
  try {
    message = Gmail.Users.Messages.get("me", messageId, { format: "full" });
  } catch (fetchErr) {
    counts.transientFailures++;
    Logger.log("message " + messageId + ": failed to fetch from Gmail, left unlabeled for retry: " + fetchErr);
    return;
  }

  // Defense in depth: the search query already excludes both labels, but
  // re-check the message's own labelIds directly in case the search index
  // is momentarily stale relative to a just-applied label.
  if (hasProcessedOrErrorLabel_(message.labelIds, processedLabelId, errorLabelId)) {
    counts.skippedAlreadyLabeled++;
    return;
  }

  var internalDateMs = parseInt(message.internalDate, 10);
  if (!isMessageEligibleByTime_(internalDateMs, config.activationInstantMs)) {
    counts.skippedBeforeActivation++;
    return;
  }

  var payload;
  try {
    var plainPart = findBodyPart_(message.payload, "text/plain");
    var htmlPart = findBodyPart_(message.payload, "text/html");
    var decodedPlain = plainPart ? decodeBase64UrlToUtf8_(plainPart.data) : "";
    var decodedHtml = htmlPart ? decodeBase64UrlToUtf8_(htmlPart.data) : "";
    // HTML-only messages are common and NOT malformed -- selectBodyText_
    // falls back to a mechanical (non-AI) HTML-to-text conversion only
    // when there is no text/plain part at all; an empty plain-text body
    // is sent as empty, never treated as "missing" and backfilled from HTML.
    var decodedBody = selectBodyText_(!!plainPart, decodedPlain, !!htmlPart, decodedHtml);
    payload = buildWebhookPayload_(message, decodedBody, { maxBodyChars: config.maxBodyChars });
  } catch (parseErr) {
    // This message's own content could not be turned into a valid payload
    // (e.g. corrupt/unwalkable MIME data) -- a property of this message,
    // not the network. Retrying would hit the same error every run,
    // forever, with zero chance of succeeding, so this is flagged the same
    // way a permanent webhook rejection is: labeled and left for a human,
    // not silently retried on an unbounded loop.
    counts.permanentFailures++;
    try {
      applyLabelToMessage_(messageId, errorLabelId);
    } catch (labelErr) {
      Logger.log("message " + messageId + ": also failed to apply the error label: " + labelErr);
    }
    Logger.log("message " + messageId + ": could not be parsed into a valid payload, labeled '" + ERROR_LABEL_NAME + "': " + parseErr);
    return;
  }

  try {
    var result = postToEmailIntakeWebhook_(config, payload);
    var outcome = classifyHttpOutcome_(result.code);

    if (outcome === "success") {
      counts.consecutivePermanentFailures = 0;
      applyLabelToMessage_(messageId, processedLabelId);
      if (result.body && result.body.already_processed) {
        counts.submittedAlreadyProcessed++;
        Logger.log("message " + messageId + ": already_processed=true (safe replay) -> labeled processed.");
      } else {
        counts.submittedNew++;
        Logger.log(
          "message " + messageId + ": accepted -> intake_id=" + (result.body && result.body.intake_id) + ", labeled processed."
        );
      }
    } else if (outcome === "auth_fatal") {
      counts.authFatal = true;
      Logger.log(
        "message " +
          messageId +
          ": webhook returned HTTP " +
          result.code +
          " (auth failure) -- STOPPING this run. Check EMAIL_INTAKE_WEBHOOK_TOKEN matches Railway's " +
          "EMAIL_INTAKE_WEBHOOK_TOKEN exactly. This message was left unlabeled and will be retried once fixed."
      );
      // No label applied -- this is a config problem, not a bad message.
    } else if (outcome === "permanent") {
      counts.permanentFailures++;
      counts.consecutivePermanentFailures++;
      applyLabelToMessage_(messageId, errorLabelId);
      // Deliberately logs only the status code, never result.body: a 422's
      // error detail can legitimately echo back the actual field VALUE that
      // failed validation (e.g. Pydantic's "input" field), which could be a
      // fragment of this message's own subject/sender/body text -- an
      // operator who needs the exact backend error detail should check
      // Railway's own logs for this request, not this script's log.
      Logger.log(
        "message " +
          messageId +
          ": webhook rejected with HTTP " +
          result.code +
          " (permanent) -> labeled '" +
          ERROR_LABEL_NAME +
          "', will NOT be retried automatically. See Railway's backend logs for the exact rejection reason."
      );
      if (shouldTreatPermanentStreakAsSystemic_(counts.consecutivePermanentFailures, PERMANENT_FAILURE_STREAK_THRESHOLD)) {
        counts.systemicPermanentSuspected = true;
        Logger.log(
          "SUSPECTED SYSTEMIC ISSUE: " +
            counts.consecutivePermanentFailures +
            " consecutive messages were permanently rejected (400/404/422) -- stopping the rest of this run rather " +
            "than continuing to label more messages as individually bad. Check EMAIL_INTAKE_WEBHOOK_URL and the " +
            "backend's deploy/contract before assuming these are truly unrelated bad messages."
        );
      }
    } else {
      counts.consecutivePermanentFailures = 0;
      counts.transientFailures++;
      Logger.log(
        "message " +
          messageId +
          ": transient failure (HTTP " +
          result.code +
          (result.error ? ", error=" + result.error : "") +
          ") -- left unlabeled, will retry next run."
      );
    }
  } catch (err) {
    // Anything unexpected while calling the webhook or applying the
    // success label (e.g. a labeling call itself failing right after a
    // successful POST -- the audited "crash before label" scenario) fails
    // safely: counted, logged with the message id only (never body
    // content), no exception propagates. Leaving it unlabeled means the
    // next run resubmits; the backend's own gmail_message_id idempotency
    // (already_processed=true) makes that resubmission harmless.
    counts.transientFailures++;
    Logger.log("message " + messageId + ": unexpected error, left unlabeled for retry: " + err);
  }
}

// ---- Manual, safe verification helper (no Gmail access at all) -----------

/**
 * Run manually from the script editor to verify Script Properties, the
 * webhook URL, and the token all work together -- BEFORE ever touching
 * Gmail. Unlike ITF's sendTestPing, the Email Intake webhook has no
 * dry_run mode (Phase 1 never needed one: it only ever creates a proposal
 * for human review, never a CRM write, so a synthetic test item is
 * inherently safe to leave behind) -- running this WILL create one real,
 * obviously-fake Email Intake item in the review queue. Reject it in the
 * CRM's Email Intake queue afterward; it never touches any CRM contact
 * either way.
 */
function verifyWebhookConnectivity() {
  var config = getConfig_();
  var payload = {
    gmail_message_id: "apps-script-connectivity-test-" + new Date().getTime(),
    gmail_thread_id: null,
    sender: "Apps Script Connectivity Test <apps-script-test@astronomic.com>",
    recipients: ["data@astronomic.com"],
    subject: "[Apps Script Connectivity Test -- safe to reject]",
    body_text: "This is a synthetic connectivity test sent directly by verifyWebhookConnectivity(). No real Gmail message was read. Safe to Reject in the Email Intake queue.",
    received_at: new Date().toISOString(),
    attachments: [],
  };
  var result = postToEmailIntakeWebhook_(config, payload);
  Logger.log(
    "verifyWebhookConnectivity result: code=" +
      result.code +
      " body=" +
      JSON.stringify(result.body) +
      (result.error ? " error=" + result.error : "") +
      " -- if code is 200, go Reject this item in the Email Intake queue."
  );
}

// ---- Trigger installation (NOT invoked by anything in this project) ------

/**
 * Creates the time-driven trigger that calls runEmailIntakeSync() on a
 * schedule. Included in source for completeness/review, exactly like
 * ITF's installFormSubmitTrigger_ -- NOT called by any other function
 * here, and must not be run until explicit go-ahead per the Phase 2A
 * launch checklist in SETUP.md. Refuses to run if a trigger already calls
 * runEmailIntakeSync (Apps Script does not dedupe triggers by function
 * name on its own, so calling this twice would otherwise silently double
 * the effective run frequency) -- delete the existing trigger first under
 * Triggers (clock icon) if you actually want to change the interval.
 */
function installTimeDrivenTrigger_(everyMinutes) {
  var minutes = everyMinutes || 15;
  var existing = ScriptApp.getProjectTriggers().filter(function (t) {
    return t.getHandlerFunction() === "runEmailIntakeSync";
  });
  if (existing.length > 0) {
    Logger.log(
      "installTimeDrivenTrigger_ ABORTED: " +
        existing.length +
        " trigger(s) already call runEmailIntakeSync -- refusing to create a duplicate. Delete the existing " +
        "one(s) under Triggers (clock icon) first if you want to change the interval, then run this again."
    );
    return;
  }
  ScriptApp.newTrigger("runEmailIntakeSync").timeBased().everyMinutes(minutes).create();
  Logger.log("Time-driven trigger installed (every " + minutes + " minutes). Verify under Triggers (clock icon) in the editor.");
}
