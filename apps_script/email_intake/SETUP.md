# Email Intake Phase 2A -- Gmail transport bridge: setup

Not deployed or activated yet. This is the exact procedure to follow when
you (Chris) are ready to go live -- nothing here has been run against the
real `data@astronomic.com` mailbox. No Gmail trigger currently exists.

## 1. Where to create the script

This is a **standalone** Apps Script project -- not bound to a Sheet or
Form, unlike the ITF bridge in `apps_script/`. That's because it needs
Gmail access and a time-driven trigger, which don't need (and shouldn't
have) any Sheet/Form attached.

1. While logged in to Google **as `data@astronomic.com`** (this matters --
   see the note below), go to script.google.com > New project.
2. Rename the project (e.g. "Email Intake Gmail Bridge").
3. Delete the default `myFunction()` boilerplate placeholder file.
4. Create two script files with this repo's content:
   - One named `logic` -- paste in the **entire contents** of
     `apps_script/email_intake/logic.js` from this repo.
   - One named `Code` -- paste in the **entire contents** of
     `apps_script/email_intake/Code.gs` from this repo.

   The script editor's "+ > Script" option in some Apps Script editor
   versions may save your file as `logic.gs` even though you typed
   `logic` (or `logic.js`) -- that's fine and expected; **only the pasted
   content matters, not the exact file name or extension shown in the
   editor's file list.** Apps Script shares one global scope across every
   script file in a project, so `Code.gs` can call every function defined
   in `logic`'s file directly, with no import statement, regardless of
   what the two files ended up named. (This repo keeps the pure-logic
   file as a literal `.js` file specifically so it can ALSO be run
   unmodified under plain Node for its unit tests -- that's a convenience
   for this repo, not a requirement Apps Script itself imposes.)

**Why it must be created as `data@astronomic.com`, not your own account:**
Apps Script's `GmailApp`/`Gmail` services always operate on the Gmail
mailbox of whoever is currently logged in when the script runs -- there is
no "target mailbox" parameter to set. If you create/authorize this script
under your own Google account, it will scan **your** mail, not the intake
address's. If you don't have direct login access to `data@astronomic.com`,
an account admin can either grant you delegated access to that mailbox, or
log in to it directly to set this up.

## 2. Enable the Gmail API advanced service (one extra click)

This script deliberately uses the **advanced `Gmail` service**, not the
simpler classic `GmailApp` service, so that the "processed" label can be
applied to one individual message rather than an entire thread -- see the
long comment at the top of `Code.gs` for exactly why that distinction
matters (a thread can get new messages long after its first message was
already labeled).

In the script editor: left sidebar > **Services** (+ icon) > find
**Gmail API** > Add. No separate Google Cloud project setup is needed for
this use case -- Apps Script provisions one automatically.

## 3. Script Properties (secrets and configuration, never in source)

Project Settings (gear icon) > Script Properties > Add script property.
Add all of these:

| Property | Example value | Required? |
|---|---|---|
| `EMAIL_INTAKE_WEBHOOK_URL` | `https://<your-railway-domain>/sync/email-intake` | Yes |
| `EMAIL_INTAKE_WEBHOOK_TOKEN` | the same value as Railway's `EMAIL_INTAKE_WEBHOOK_TOKEN` env var | Yes |
| `EMAIL_INTAKE_ACTIVATION_DATE` | e.g. `2026-08-13T00:00:00+08:00` -- **your real go-live instant, decided in step 8 below** | Yes -- the script refuses to run at all without this |
| `EMAIL_INTAKE_ENVIRONMENT_LABEL` | `production` (or `staging` / `local-test`) | Recommended -- purely a log label, never affects behavior |
| `EMAIL_INTAKE_MAX_MESSAGES_PER_RUN` | `20` (the default if unset) | Optional |
| `EMAIL_INTAKE_MAX_BODY_CHARS` | `100000` (the default if unset) | Optional |

Notes:
- `EMAIL_INTAKE_ACTIVATION_DATE` **must** include a time and an
  offset/zone (e.g. `+08:00` or `Z`) -- a bare calendar date like
  `2026-08-13` is rejected on purpose (the script fails closed rather than
  guessing what time zone you meant).
- Generate the token with something like `openssl rand -hex 32` if you
  haven't already reused the one from Phase 1's local testing. Treat it
  like a password.

## 4. Verify connectivity BEFORE touching Gmail at all

In the script editor, select `verifyWebhookConnectivity` from the function
dropdown and click Run.

This sends one obviously-fake, synthetic payload **directly** to the
webhook -- it does not read Gmail at all. Unlike the ITF bridge, the Email
Intake webhook has no `dry_run` mode (it doesn't need one: Phase 1 never
writes to the CRM on ingestion, only ever creates a review-queue item), so
this **will** create one real Email Intake item with subject
`[Apps Script Connectivity Test -- safe to reject]`. Check:

1. The script editor's execution log shows `code=200` -- confirms the
   token and URL are both correct.
2. Open the CRM's Email Intake queue (Settings > Email Intake) and confirm
   the test item appeared.
3. Click into it and **Reject** it. It never touched any CRM contact
   either way, but there's no reason to leave a fake item sitting in the
   review queue.

The first time you run any function, Google will show an authorization
prompt: "This app isn't verified" (expected for a private script you wrote
yourself) -- click Advanced > Go to (your project name) > Allow. At this
step, the permissions requested are just **connect to an external
service** (`UrlFetchApp`) -- Gmail access isn't requested yet because this
function never touches Gmail.

## 5. Decide your activation timestamp

Pick the exact moment you want to start ingesting mail -- everything
received before this instant is permanently ignored by this script (there
is no backfill function). Set `EMAIL_INTAKE_ACTIVATION_DATE` to that value
now, in step 3 above, well before you run anything that touches Gmail.

## 6. Test-run against real Gmail, manually, once

Select `runEmailIntakeSync` from the function dropdown and click Run.

The first time, Google will show the authorization prompt again -- this
time for Gmail access. The scope requested is **read, compose, send, and
permanently delete your email from Gmail** (this is the standard label for
the `gmail.modify` scope the Gmail API needs to add labels -- this script
never sends, deletes, or composes anything; it only reads messages and
adds two labels).

Before running this against your real mailbox for the first time:

1. Make sure `EMAIL_INTAKE_ACTIVATION_DATE` is set to your real intended
   go-live instant (step 5).
2. Send **one** real test email to `data@astronomic.com` from an address
   you control, with a real timestamp after your activation instant.
3. Run `runEmailIntakeSync` manually.
4. Check the execution log for a line like:
   ```
   runEmailIntakeSync finished -- candidates=1 submittedNew=1 submittedAlreadyProcessed=0 skippedBeforeActivation=0 skippedAlreadyLabeled=0 permanentFailures=0 transientFailures=0
   ```
5. Confirm in Gmail that your test message now carries the
   `crm-intake-processed` label.
6. Confirm in the CRM's Email Intake queue that exactly one new item
   appeared for that message.
7. Confirm the CRM contact it matched (if any) is completely unchanged --
   Email Intake only ever proposes; nothing here can write to a contact.
8. Run `runEmailIntakeSync` again, manually, a second time. Confirm the
   log now shows `candidates=0` (the message is already labeled) and that
   **no second Email Intake item was created**. This is the "webhook
   succeeded, label application happened, retry is a no-op" case.

## 7. Install the recurring trigger (only after step 6 passes)

Either:
- **UI**: clock icon (Triggers) in the left sidebar > + Add Trigger >
  function: `runEmailIntakeSync`, event source: `Time-driven`, type:
  `Minutes timer` > every 15 minutes (or your preferred interval -- see
  "recommended frequency" below) > Save.
- **Code**: select `installTimeDrivenTrigger_` in the function dropdown
  (edit the `everyMinutes` argument if you want something other than the
  15-minute default), click Run, once.

**Recommended frequency:** every 10-15 minutes is a reasonable starting
point -- frequent enough that a reviewer sees new proposals promptly,
infrequent enough to stay well clear of Apps Script's quotas and the
6-minute per-execution cap. `LockService` already prevents two runs from
overlapping if a run is still finishing when the next one would start, so
a shorter interval is not unsafe, just unnecessary.

## 8. Railway / backend configuration

Set one environment variable on the Railway service (if you haven't
already, from local Phase 1 testing):

| Variable | Value |
|---|---|
| `EMAIL_INTAKE_WEBHOOK_TOKEN` | the same random secret you put in the script's `EMAIL_INTAKE_WEBHOOK_TOKEN` property |

No other new env vars, no CORS changes (Apps Script's `UrlFetchApp` is a
server-to-server call, not a browser `fetch`), no OAuth, no service
account JSON.

## 9. Verifying the label afterward

At any point, in Gmail, search `label:crm-intake-processed` to see every
message this script has successfully forwarded.

## 9a. Finding and recovering error-labeled messages

Search `label:crm-intake-error` in Gmail to see every message this script
permanently gave up on (a 400/404/422 rejection from the webhook, or a
message whose content this script genuinely could not parse -- see
Code.gs's `ERROR_LABEL_NAME` comment for why these stop retrying
automatically rather than looping forever). Nothing here is deleted --
the original email is completely intact, just tagged.

To investigate and safely retry one of these messages:

1. In Gmail, search `label:crm-intake-error` and open the message.
2. Check the script editor's execution log (View > Executions, or the
   Apps Script dashboard's Executions tab) around the time that message
   arrived -- the log line for it names the message ID and HTTP status,
   e.g. `webhook rejected with HTTP 422 ... See Railway's backend logs
   for the exact rejection reason`. Check Railway's own logs for that
   request if you need the precise validation error (deliberately not
   duplicated into this script's log -- see the privacy note in the
   Phase 2A audit report).
3. If the log also shows `SUSPECTED SYSTEMIC ISSUE` (3 or more messages
   in a row were rejected), the problem is very likely
   `EMAIL_INTAKE_WEBHOOK_URL` being wrong, or a backend deploy/contract
   issue affecting every message -- fix that first, or every retried
   message will just fail again the same way.
4. Once you believe the underlying problem is fixed, remove the label
   from the message: open it in Gmail, click the label icon (or the
   three-dot "More" menu) above the message, find `crm-intake-error` in
   the label list, and click it to un-check/remove it. (Desktop Gmail:
   the label icon looks like a small tag; it's in the toolbar above the
   open message, next to Archive/Delete.)
5. The message is now eligible again and will be picked up automatically
   on the next scheduled run (or run `runEmailIntakeSync` manually to
   retry it immediately).

Removing `crm-intake-processed` from a message the same way makes it
eligible for reprocessing too, though this is rarely needed since a
processed message already has a corresponding Email Intake item.

## 10. What NOT to do yet

- Do not run `installTimeDrivenTrigger_` (or add the trigger via the UI)
  until step 6's manual test has fully passed.
- Do not set `EMAIL_INTAKE_ACTIVATION_DATE` to a date in the past relative
  to when you actually flip this on -- that would let genuinely old mail
  through the activation boundary.
- Do not point this script's `EMAIL_INTAKE_WEBHOOK_URL` at production
  until you've verified it against a non-production URL first, if you
  have one available; if not, step 4 and step 6 above are your safety net
  before this ever runs on a schedule.

---

## Running the unit tests

`logic.js`'s pure functions have their own Node test suite that needs no
Gmail account, no Apps Script project, and no network access:

```bash
node --test apps_script/email_intake/logic.test.js
```
