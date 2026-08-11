/**
 * ITF (Investor Thesis Form) -> CRM webhook bridge.
 *
 * Bound to the ITF response Google Sheet (Extensions > Apps Script from
 * within that Sheet -- see apps_script/SETUP.md for exact steps). An
 * installable "On form submit" trigger calls onFormSubmitTrigger(e) once
 * per real Form submission; this script's ONLY jobs are:
 *
 *   1. Read the submitted row (e.values) and the Sheet's header row.
 *   2. POST them, positionally, to the CRM's POST /sync/itf-contact webhook.
 *   3. Log the result.
 *
 * It contains NO field-mapping, classification, deduplication, or merge
 * logic -- all of that lives in the backend (CrmImportService.import_one_row
 * and friends), which is the single source of truth for both this and the
 * CSV import path. This script never writes to the response Sheet, never
 * modifies the Form, and never hardcodes a secret -- CRM_API_URL and
 * CRM_API_TOKEN live in PropertiesService, configured once via
 * setScriptProperties() below (run it manually, once, from the script
 * editor -- see SETUP.md).
 */

// ---- Configuration (read from Script Properties, never hardcoded) --------

function getConfig_() {
  var props = PropertiesService.getScriptProperties();
  var apiUrl = props.getProperty('CRM_API_URL');
  var apiToken = props.getProperty('CRM_API_TOKEN');
  if (!apiUrl || !apiToken) {
    throw new Error(
      'CRM_API_URL / CRM_API_TOKEN are not set. Run setScriptProperties_() ' +
      'once from the script editor (see SETUP.md), or set them by hand under ' +
      'Project Settings > Script Properties.'
    );
  }
  return { apiUrl: apiUrl, apiToken: apiToken };
}

/**
 * Run this ONCE, manually, from the script editor (select this function in
 * the toolbar dropdown, click Run) to store your CRM URL + token. Edit the
 * two values below before running, then you may delete/blank the token
 * value here if you want -- it's already saved in Script Properties at that
 * point and does not need to stay in source.
 */
function setScriptProperties_() {
  PropertiesService.getScriptProperties().setProperties({
    CRM_API_URL: 'https://YOUR-RAILWAY-APP.up.railway.app/sync/itf-contact',
    CRM_API_TOKEN: 'PASTE-THE-SAME-VALUE-AS-RAILWAYS-ITF_WEBHOOK_TOKEN-HERE',
  });
  Logger.log('Script Properties set. You can now blank out the token above.');
}

// ---- Trigger entry point --------------------------------------------------

/**
 * Installable "On form submit" trigger target, bound to the Sheet (not the
 * Form) -- see SETUP.md for why that matters. `e.values` is the submitted
 * row in COLUMN ORDER (safe against duplicate question titles); `e.range`
 * is the newly-appended row's Range, so e.range.getRow() is the row number.
 * `e.response` is generally NOT present for a Sheet-bound trigger (that's a
 * Form-bound-trigger-only property) -- this code checks for it defensively
 * and sends null if absent, exactly matching the backend's documented
 * fallback (row_number + content hash is sufficient on its own).
 */
function onFormSubmitTrigger(e) {
  try {
    var sheet = e.range.getSheet();
    var rowNumber = e.range.getRow();
    var numColumns = e.range.getLastColumn();
    var headers = sheet.getRange(1, 1, 1, numColumns).getValues()[0];
    var values = e.values || [];

    var responseId = null;
    try {
      if (e.response && typeof e.response.getId === 'function') {
        responseId = e.response.getId();
      }
    } catch (idErr) {
      responseId = null; // defensive -- never let this block the submission
    }

    var payload = {
      source: 'itf',
      row_number: rowNumber,
      response_id: responseId,
      headers: headers,
      values: values,
    };

    var result = postToCrmWithRetry_(payload);
    logResult_(rowNumber, result);
  } catch (err) {
    // Never let an exception here silently vanish -- Apps Script's own
    // execution log (View > Executions) is the audit trail per the "no
    // writes to the response Sheet" constraint.
    Logger.log('onFormSubmitTrigger FAILED for row ' + (e && e.range ? e.range.getRow() : '?') + ': ' + err);
  }
}

// ---- HTTP -------------------------------------------------------------

var MAX_ATTEMPTS_ = 3;
var RETRY_DELAY_MS_ = 2000; // fixed backoff -- Apps Script trigger runtime is capped at 6 minutes total

/**
 * POSTs once, retrying only on responses that are plausibly transient
 * (network failure, 5xx, or Railway returning no response at all mid-
 * restart) -- a 400/401/403/422 is a permanent rejection (bad payload or
 * bad token) and is NOT retried, since retrying an auth or validation
 * failure only risks masking a real misconfiguration behind repeated log
 * noise. The backend's own idempotency ledger (row_number + content hash)
 * makes a retried POST safe even if an earlier attempt actually succeeded
 * but the response was lost -- the worst case is a redundant "already_
 * processed" response, never a duplicate contact.
 */
function postToCrmWithRetry_(payload, urlOverride) {
  var config = getConfig_();
  var url = urlOverride || config.apiUrl;
  var options = {
    method: 'post',
    contentType: 'application/json',
    headers: { Authorization: 'Bearer ' + config.apiToken },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true, // so a non-2xx is inspectable, not thrown
  };

  var lastError = null;
  for (var attempt = 1; attempt <= MAX_ATTEMPTS_; attempt++) {
    try {
      var response = UrlFetchApp.fetch(url, options);
      var code = response.getResponseCode();

      if (code >= 200 && code < 300) {
        return { ok: true, code: code, body: safeParseJson_(response.getContentText()) };
      }
      if (code >= 400 && code < 500) {
        // Permanent rejection -- do not retry. Body is safe to log; it is
        // the CRM's own error detail, never the token (the token is a
        // REQUEST header we sent, not something the CRM echoes back).
        return { ok: false, code: code, body: safeParseJson_(response.getContentText()), permanent: true };
      }
      // 5xx or unexpected -- fall through to retry.
      lastError = 'HTTP ' + code + ': ' + response.getContentText();
    } catch (fetchErr) {
      lastError = String(fetchErr); // network-level failure (CRM/Railway unreachable)
    }

    if (attempt < MAX_ATTEMPTS_) {
      Utilities.sleep(RETRY_DELAY_MS_ * attempt); // linear backoff
    }
  }

  return { ok: false, code: null, body: null, error: lastError };
}

function safeParseJson_(text) {
  try {
    return JSON.parse(text);
  } catch (e) {
    return { raw: text };
  }
}

function logResult_(rowNumber, result) {
  if (result.ok) {
    var status = result.body && result.body.status ? result.body.status : 'unknown';
    var contactId = result.body && result.body.contact_id ? result.body.contact_id : 'n/a';
    Logger.log('Row ' + rowNumber + ' -> ' + status + ' (contact_id=' + contactId + ')');
  } else if (result.permanent) {
    Logger.log(
      'Row ' + rowNumber + ' REJECTED (HTTP ' + result.code + '): ' +
      JSON.stringify(result.body) + ' -- not retried, needs manual investigation.'
    );
  } else {
    Logger.log(
      'Row ' + rowNumber + ' FAILED after ' + MAX_ATTEMPTS_ + ' attempts: ' + result.error +
      ' -- CRM may be down; this submission was NOT confirmed processed.'
    );
  }
}

// ---- Manual, safe verification helpers (no real Sheet/Form interaction) --

/**
 * Run this manually from the script editor to verify PropertiesService,
 * UrlFetchApp, and the CRM webhook all work together -- BEFORE installing
 * the real onFormSubmit trigger. ALWAYS calls with ?dry_run=true (hardcoded
 * below, not a config toggle) -- this sends a synthetic, obviously-fake
 * payload and confirms the whole auth/plumbing chain works, WITHOUT ever
 * creating a real CRM contact or writing an ingestion-log entry. Touches no
 * Google resource at all -- only your CRM, and only a classification dry
 * run at that.
 */
function sendTestPing() {
  var config = getConfig_();
  var payload = {
    source: 'itf',
    row_number: 999999, // clearly out-of-range test marker, never a real Sheet row
    response_id: 'apps-script-test-ping',
    headers: ['Timestamp', 'First Name', 'Last Name', 'Email Address'],
    values: [new Date().toISOString(), 'Test', 'Ping', 'apps-script-test@example.com'],
  };
  var result = postToCrmWithRetry_(payload, config.apiUrl + '?dry_run=true');
  Logger.log('sendTestPing (dry_run=true, no CRM write) result: ' + JSON.stringify(result));
}

// TEMPORARY pre-activation verification tool -- safe to delete once the first real
// production submission (via the installed onFormSubmit trigger) has been verified
// end-to-end. It exists only to let you inspect the ACTUAL response Sheet's real
// row 2 -- headers, mapping, dedup result -- through the exact same payload shape
// and dry_run path onFormSubmitTrigger uses, before committing to live activation.

/**
 * TEMPORARY, pre-activation only -- remove after the first successful real
 * production test (see SETUP.md). Reads the real Sheet's row 1 (headers) and
 * row 2 (the one existing real response) directly -- READ-ONLY: only
 * getRange(...).getDisplayValues() calls below, no setValue/appendRow/
 * deleteRow/clear anywhere in this function, no Form interaction, no
 * trigger installed or modified. Builds the identical payload shape
 * onFormSubmitTrigger sends (source/row_number/response_id/headers/values)
 * and POSTs it to the same CRM_API_URL with ?dry_run=true forced, so
 * running this can never create a CRM contact or write an ingestion-log
 * entry, however many times you run it.
 *
 * Uses getDisplayValues() rather than getValues() deliberately: a real
 * onFormSubmit event's e.values are the form's own formatted text (e.g. the
 * Timestamp column looks like "8/6/2026 17:46:14"), not a native Date
 * object -- getValues() would return a JS Date for that column instead,
 * which JSON.stringify's to a different, ISO-formatted string and would
 * make this dry run's payload subtly unlike a real submission's. Display
 * values match the real trigger's shape far more closely.
 *
 * Row number is hardcoded to 2 -- the one existing real response -- exactly
 * as instructed, no user input involved.
 */
function sendExistingRowDryRun() {
  var TAB_NAME = 'Form Responses 1';
  var ROW_NUMBER = 2; // hardcoded: the Sheet's one existing real response row

  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(TAB_NAME);
  if (!sheet) {
    Logger.log('sendExistingRowDryRun FAILED: tab "' + TAB_NAME + '" not found in this spreadsheet.');
    return;
  }

  var numColumns = sheet.getLastColumn();
  var headers = sheet.getRange(1, 1, 1, numColumns).getDisplayValues()[0];
  var values = sheet.getRange(ROW_NUMBER, 1, 1, numColumns).getDisplayValues()[0];

  var payload = {
    source: 'itf',
    row_number: ROW_NUMBER,
    response_id: null, // no real Form-submission event exists here, same as a Sheet-bound trigger normally reports
    headers: headers,
    values: values,
  };

  var config = getConfig_();
  var result = postToCrmWithRetry_(payload, config.apiUrl + '?dry_run=true');
  Logger.log(
    'sendExistingRowDryRun (tab="' + TAB_NAME + '", row=' + ROW_NUMBER +
    ', dry_run=true, no CRM write, no Sheet write) result: ' + JSON.stringify(result)
  );
}

/**
 * Optional convenience: creates the installable "On form submit" trigger
 * bound to this Sheet, targeting onFormSubmitTrigger. Run manually, once,
 * from the script editor -- equivalent to (and a substitute for) using the
 * Triggers UI described in SETUP.md. Safe to run more than once only if you
 * delete the old trigger first (Apps Script does not dedupe triggers by
 * function name) -- check Triggers (clock icon) in the editor before
 * re-running.
 */
function installFormSubmitTrigger_() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  ScriptApp.newTrigger('onFormSubmitTrigger').forSpreadsheet(ss).onFormSubmit().create();
  Logger.log('Trigger installed. Verify under Triggers (clock icon) in the script editor.');
}
