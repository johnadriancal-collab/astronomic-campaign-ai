# AstroHub Governance Decisions

This file is a durable, versioned record of narrow exceptions to Astronomic's
standing org-level policies (as set in ASTRONOMIC_MASTER.md / org
instructions). It exists because ASTRONOMIC_MASTER.md itself lives in Google
Drive, which this environment cannot write to directly — each entry here is
the exact text for Chris to transcribe into ASTRONOMIC_MASTER.md. Until that
transcription happens, THIS file is the source of truth an automated task in
this repo should consult before treating a Campaign Manager send as a policy
violation.

Do not treat an entry here as license to generalize beyond its stated scope.

---

## Campaign Manager — First External Pilot (2026-09-17)

**Decision:** For this approved pilot only, AstroHub Campaign Manager may
send directly through the Gmail API instead of QuickMail.

**Scope (exact, non-transferable):**
- Campaign ID: `1fe98436-b4e5-4459-b415-20714726f469`
- Mailbox: `victoria@useastronomic.com`
- Exactly 5 approved recipients:
  - johnadriancal@astronomic.com
  - brendan@bizdevdinners.com
  - victoria@astronomicconnect.com
  - chris@galaxysway.com
  - maximusromy1165@gmail.com
- 3-step sequence: Day 0 / Day 3 / Day 6
- No other Campaign Manager campaign is authorized by this exception.

**The general rule remains:** QuickMail is the default/approved sending
system unless a Campaign Manager campaign has been explicitly approved as an
exception, recorded here with its own campaign ID, mailbox, and recipient
scope. This entry does not weaken or remove the QuickMail-only rule for any
other outreach.

**Status:** Approved by the user (John Adrian Cal) in-session on
2026-09-17. Pending transcription into the canonical ASTRONOMIC_MASTER.md
(Google Drive) by Chris.

---

## Controlled Astronomic Mail Bounce Test Exception (2026-09-18)

**Decision:** For this one controlled send only, AstroHub Campaign Manager
may send directly through the Gmail API instead of QuickMail, to
production-verify the newly deployed Bounce Tracking feature.

**Scope (exact, non-transferable):**
- Purpose: validate the full real chain -- Astronomic Mail send → outbound
  RFC Message-ID → recipient-server rejection → DSN returned to the
  sending mailbox → Gmail History detection → DSN parsing → attribution
  → MailBounce persistence → Bounce Rate calculation.
- Exactly ONE email, ONE recipient: a controlled, confirmed-nonexistent
  address on a domain we control (verified not catch-all before sending;
  see the verification steps below).
- Dedicated test campaign only (e.g. "ZZTEST Bounce Detection Production
  Verification") -- one step, no open tracking required.
- Sending mailbox: `victoria@useastronomic.com` (the same approved test
  mailbox as the pilot exception above).
- Does NOT authorize: normal outreach beyond already-approved exceptions,
  bulk sends, additional test recipients, or repeated bounce tests
  without a fresh approval. Exhausted after this single send.
- The live 5-person external pilot (campaign
  `1fe98436-b4e5-4459-b415-20714726f469`) is untouched by this exception
  and remains governed solely by the entry above.

**The general rule remains:** QuickMail is the default/approved sending
system unless explicitly excepted here. This entry does not weaken or
remove the QuickMail-only rule for any other outreach.

**Status:** Approved by the user (John Adrian Cal) in-session on
2026-09-18. Pending transcription into the canonical ASTRONOMIC_MASTER.md
(Google Drive) by Chris.
