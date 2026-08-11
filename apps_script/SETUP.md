# ITF -> CRM Apps Script bridge: setup

Not deployed yet. This is the exact procedure to follow when you're ready to
activate it -- nothing here has been run against the real Sheet or Form.

## 1. Where to create the script

1. Open the real ITF response **Google Sheet** (not the Form).
2. Extensions > Apps Script. This creates a script **bound to the Sheet**
   (not standalone, not bound to the Form) -- that's the deliberate choice:
   a Sheet-bound trigger gets `e.values`/`e.range` positionally, which is
   what lets us sidestep the duplicate private/institutional question-title
   collision. Binding it to the Sheet also means it can never be attached to
   the wrong response destination.
3. Delete the default `myFunction()` boilerplate and paste in the contents
   of `apps_script/Code.gs` from this repo.

## 2. Script Properties (the secret, never in source)

In the script editor: Project Settings (gear icon) > Script Properties > Add
script property. Add two:

| Property | Value |
|---|---|
| `CRM_API_URL` | `https://<your-railway-domain>/sync/itf-contact` |
| `CRM_API_TOKEN` | the same value you set for `ITF_WEBHOOK_TOKEN` on Railway |

Alternative: select `setScriptProperties_` in the function dropdown, edit
the two placeholder values in that function, click Run once, then you can
blank the values back out in the source (they're already saved).

Generate the token value with something like:
```bash
openssl rand -hex 32
```
Set the *same* value in both places -- Railway's `ITF_WEBHOOK_TOKEN` env var
and the script's `CRM_API_TOKEN` property. Treat it like a password: it's
the only thing standing between the public internet and this webhook.

## 3. Verify before installing the real trigger

In the script editor, select `sendTestPing` from the function dropdown and
click Run. This sends one synthetic, obviously-fake row directly to your CRM
endpoint with `?dry_run=true` hardcoded -- it never touches the real Sheet
or Form, and it never creates a real CRM contact or ingestion-log entry
(nothing to clean up afterward). Check the script editor's execution log
(View > Executions, or the log panel after running) for a line like:
```
sendTestPing (dry_run=true, no CRM write) result: {"ok":true,"code":200,"body":{"status":"created","dry_run":true,"contact_id":null,...}}
```
`ok: true` and `code: 200` confirm the token was accepted and the payload was
valid. `body.status` (created/updated/possible_duplicate) is what the row
WOULD do. `body.contact_id: null` and `body.dry_run: true` confirm nothing
was written -- that's expected and correct for this synthetic test.

The first time you run any function, Google will show an authorization
prompt: "This app isn't verified" (expected for a private script you wrote
yourself) -- click Advanced > Go to (your project name) > Allow. The
permissions requested are exactly two: **see, edit, create, and delete your
spreadsheets** (needed to read the header row) and **connect to an external
service** (needed for `UrlFetchApp.fetch`). No Gmail, Drive, or Calendar
access is requested.

## 4. Install the real trigger (only once dry-run review is complete)

Either:
- **UI**: clock icon (Triggers) in the left sidebar > + Add Trigger >
  function: `onFormSubmitTrigger`, event source: `From spreadsheet`, event
  type: `On form submit` > Save.
- **Code**: select `installFormSubmitTrigger_` in the function dropdown,
  click Run, once.

Either way, Google will ask you to authorize the trigger itself (same
two-permission prompt as above) the first time it's created.

## 5. Railway / backend configuration

Set one new environment variable on the Railway service:

| Variable | Value |
|---|---|
| `ITF_WEBHOOK_TOKEN` | the same random secret you put in `CRM_API_TOKEN` above |

No other new env vars, no CORS changes needed (Apps Script's `UrlFetchApp`
is a server-to-server call, not a browser `fetch` -- CORS doesn't apply).
No OAuth, no service-account JSON, nothing else to configure.

## 6. What NOT to do yet

- Do not click Save on step 4 until the field-mapping audit has been
  reviewed and approved.
- Do not submit a real Form response to test this.
- Do not edit the response Sheet's existing rows, structure, or headers.
