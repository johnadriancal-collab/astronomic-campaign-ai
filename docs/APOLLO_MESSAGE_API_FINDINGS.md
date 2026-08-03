# Apollo message/activity API — live research findings & proposed architecture

Status: **architecture approved, nothing implemented yet.** This document
records a live-data investigation into Apollo's per-message and per-event
endpoints, and the resulting decisions on how `EmailMessage` /
`EmailMessageEvent` should eventually be shaped. **No `EmailMessage` or
`EmailMessageEvent` table, model, route, or background scheduler exists in
code as of this document** — §8 is a proposal awaiting a future
implementation turn, not a description of built code. Phase 1
(`EmailSequence`, `EmailSequenceStep`, manual sync) is **[BUILT]** and
unchanged by this research.

Every finding below is tagged:

- **LIVE** — observed directly against the real Apollo API during this
  investigation, with the exact field/value seen.
- **DOCS** — a claim from Apollo's published documentation or an earlier
  research pass, not independently confirmed live. Treat as unverified.
- **UNKNOWN** — not observed, not assumed. Do not implement against these
  until they're actually seen.

Two data sources were used and are kept clearly separate:

- **Solstice Health** — our own real sequence/campaign, built and
  activated through this app. It has generated **zero** messages so far.
  The enrollment mailbox (`65399fb6b7a25700a35bacd2`) has
  `revoked_at: 2026-07-16T11:44:21.715+00:00` — its Gmail OAuth connection
  appears revoked, which plausibly blocks Apollo from ever scheduling a
  send through it. Nothing here has been changed to work around this — no
  rebuild, no re-enrollment, no mailbox change.
- **Pre-existing, unrelated sequences** in the same Apollo account
  ("Aurora Sales Prospecting", "Supernova Sales Prospecting") — real,
  already-sent messages with thousands of delivered/opened/clicked/replied
  records, used only to observe real API shapes. **This data does not
  belong to our app and does not map to our `Lead` records** — it's cited
  here purely as evidence of what Apollo's API actually returns. All
  emails, names, subjects, and body text below are redacted.

---

## 1. Confirmed LIVE fields — `GET /emailer_messages/search`

Observed directly on real message records:

| Field | Status | Observed value(s) |
|---|---|---|
| `id` | LIVE | Apollo's message id |
| `emailer_campaign_id` | LIVE | matches `EmailSequence.apollo_sequence_id` |
| `emailer_step_id` | LIVE | matches `EmailSequenceStep.apollo_step_id` |
| `emailer_touch_id` | LIVE | present, distinct from `emailer_step_id` — see §7 |
| `contact_id` | LIVE | matches our `Lead.apollo_contact_id` |
| `status` | LIVE | only `"completed"` and `"failed"` observed |
| `created_at` | LIVE | message-record creation timestamp |
| `due_at` | LIVE | scheduled send time |
| `completed_at` | LIVE | populated on both success (`status="completed"`) and failure (`status="failed"`) |
| `failed_at` | LIVE | populated only when `status="failed"` |
| `failure_reason` | LIVE | observed value: `"Spam Blocked"` |
| `bounce` | LIVE | boolean, independent of `status` |
| `spam_blocked` | LIVE | boolean, independent of `status` |
| `replied` | LIVE | boolean, directly on the message object |
| `reply_class` | LIVE | observed value: `"not_interested"` — Apollo's own server-side reply classification |
| `provider_message_id` | LIVE | the underlying Gmail message id |
| `provider_thread_id` | LIVE | the underlying Gmail thread id |
| `conversation_id` | LIVE (present, but never populated) | `null` in every example seen, **including** a `replied=true` record — see §7 |

**Confirmed absent** (checked for directly, not present on any record, including ones matching `opened`/`clicked` filters):

- `num_opens` — **not present**
- `num_clicks` — **not present**
- `opened_at` / `clicked_at` — **not present**
- No flat engagement-count field of any kind exists on `/emailer_messages/search` results.

## 2. Confirmed LIVE fields/events — `GET /emailer_messages/{id}/activities`

Response shape, observed directly:

```json
{
  "activities": [
    {
      "type": "emailer_message_events_group",
      "event_group_type": "open",
      "emailer_message_events": [
        {
          "id": "...",
          "type": "open",
          "created_at": "2025-02-13T16:49:51.000Z",
          "contact_id": "...",
          "emailer_message_id": "...",
          "readable_user_agent": "Chrome, Windows",
          "third_party_tracking_service": null,
          "state": "TX",
          "country": "United States"
        }
      ]
    },
    {
      "type": "emailer_message_events_group",
      "event_group_type": "click",
      "emailer_message_events": [ /* same shape, type: "click" */ ]
    }
  ]
}
```

Confirmed live:

- Opens and clicks are grouped by `event_group_type` (`"open"` | `"click"`), each holding an array of individual events.
- **Each event has its own `created_at` timestamp** — multiple opens/clicks on the same message appear as multiple discrete events, not one aggregate. Observed one real message with 1 open event and 2 click events, each 1–3 seconds apart with distinct timestamps.
- Each event carries `id`, `type`, `created_at`, `contact_id`, `emailer_message_id`, `readable_user_agent`, and geolocation fields (`state`, `country`).
- There is no count field anywhere in this response either — `num_opens`/`num_clicks` would have to be **derived by counting array entries**, grouped by `event_group_type`.

## 3. Confirmed LIVE ID mappings back to our records

| Apollo field | Maps to | Verified how |
|---|---|---|
| `contact_id` | `Lead.apollo_contact_id` | Cross-referenced 3 real Solstice Health leads via `GET /leads` / `GET /leads/{id}` — exact match |
| `emailer_campaign_id` | `EmailSequence.apollo_sequence_id` | Matches the id already synced by Phase 1 |
| `emailer_step_id` | `EmailSequenceStep.apollo_step_id` | Matches the ids already synced by Phase 1 |

Caveat: since Solstice Health has zero real messages, this mapping was verified against the **shape** of the ids (format, and that `contact_id` for the same person is stable across endpoints), not against a live message actually belonging to one of our own leads.

## 4. Fields that should NOT be assumed

These appeared in earlier (pre-live) research, based on documentation summaries, and are now confirmed **wrong** or unconfirmed against live data:

- ❌ `num_opens` / `num_clicks` as flat fields on the message object — **confirmed absent** live, on both `/search` and `/activities`. Do not add these as columns; if built, they must be a derived count.
- ❌ "No per-event timestamps on opens/clicks" — **confirmed wrong** live. Every open/click event has its own `created_at`.
- ⚠️ Do not assume `status` has more values than `"completed"`/`"failed"` — no other value has been observed live, even though a "scheduled"/pending-style value is plausible.
- ⚠️ Do not assume `conversation_id` ever populates — it was `null` on every record seen, including a `replied=true` one.
- ⚠️ Do not assume a reply's `emailer_message_id` reliably resolves back to the original outbound message — see the anomaly in §7.

## 6. Confirmed LIVE — pagination behavior of `/emailer_messages/search`

Checked directly (metadata/ids only, no message content) as input to the
sync design in §8:

- Standard `page` / `per_page` request params, same convention as
  `search_people`/`search_companies` elsewhere in this codebase. `page=1`
  and `page=2` with `per_page=2` returned fully disjoint id sets — offset
  paging works.
- `per_page=100` was accepted and returned 100 records — no smaller cap
  hit at that size.
- **No `pagination`/total-count envelope.** The response is
  `{"emailer_messages": [...], "emailer_steps": [...], "breadcrumbs": [],
  "num_fetch_result": null}` — unlike `search_people`, there is no
  `total_entries`/`total_pages` field. `num_fetch_result` and
  `breadcrumbs` were empty/null on every call made. **This means the only
  way to know pagination is complete is an empty (or short) page**, not a
  total count read up front.
- **No supported sort parameter.** Passing `sort_by_field: "created_at"`
  returned a live `422` (`"No mapping found for [created_at] in order to
  sort on"`). Apollo does not support sorting this endpoint by
  `created_at` — default ordering must be used as-is; do not build a
  "sort by newest" assumption into the sync.
- Default ordering was stable across immediate repeated identical calls
  (same ids, same order) in a quiet account. This does **not** prove
  ordering is stable while new messages are actively being created
  concurrently — offset pagination with an unstable sort order is a
  known way to skip or duplicate rows if writes happen mid-page. See the
  resumability design in §8.5.
- `emailer_campaign_ids: [...]` **does scope results** — filtering by a
  nonexistent campaign id returned `0` records, consistent with it being a
  real, applied filter (used already for `search_sequences`, so the same
  filter shape carries over to messages).

## 7. Remaining unknowns

- **Complete `status` enum.** Only `"completed"` and `"failed"` have been observed live. Whether a pending/scheduled/in-progress value exists is **UNKNOWN** — nothing in any account currently has an in-flight message to observe.
- **`emailer_touch_id` semantics.** Confirmed live to exist and to be distinct from `emailer_step_id`, sitting between step and message in the hierarchy (`emailer_campaign_id` → `emailer_step_id` → `emailer_touch_id` → message). Its exact purpose (e.g. A/B variant identifier) is **UNKNOWN** — not confirmed against a real multi-touch/variant example.
- **Whether `conversation_id` ever populates.** **UNKNOWN** — null in every example observed so far, including replies.
- **Reliable reply → outbound-message mapping.** **UNKNOWN, with a specific open anomaly.** Querying `/emailer_messages/{id}/activities` for a message matched by an `emailer_message_stats[]=replied` filter returned a structurally different record: `type: "downloaded_email"` instead of the usual outbound-message shape, with `emailer_campaign_id: null` and `emailer_step_id: null`, and belonging to a different `account_id`/`user_id` than the one being queried. This could mean (a) inbound replies are represented as a separate, disconnected message type not tied back to the outbound campaign/step, or (b) a filter-scoping quirk in Apollo's API. Investigation was deliberately stopped here rather than digging further into what looked like unrelated account data. **Do not assume replies cleanly join back to the originating `EmailMessage` until this is resolved.**
- **Whether the `replied` field on `/emailer_messages/search` consistently maps to the outbound message.** Partially confirmed: `replied`/`reply_class` are real fields directly on the outbound message object in every non-anomalous record seen. But given the anomaly above, it's **UNKNOWN** whether `replied=true` is always reliable, or whether some replies only surface via the disconnected `downloaded_email`-shaped record instead.
- **Solstice-Health-specific validation.** Nothing above has been verified against our own sequence's real data, since it has produced zero messages. All of it comes from unrelated pre-existing sequences in the same account. This should be re-verified once Solstice Health's mailbox is reconnected (or a healthy mailbox is used) and real sends occur.

---

## 8. Finalized architecture decisions (approved — not yet implemented)

These are decided; nothing below has been built. They constrain whatever
future turn implements `EmailMessage`.

### 8.1 Mailbox — deferred, untouched

The revoked Solstice Health mailbox and the existing campaign's enrollment
are explicitly out of scope here and were not touched by this
investigation. The sending-account decision will be made separately,
before any real Solstice-specific message data can exist.

### 8.2 `EmailMessage.status` — open string, not a closed enum

Only `"completed"`/`"failed"` have ever been observed live (§1, §7). A
closed `Enum` would either reject or silently coerce any future value
Apollo returns that hasn't been seen yet (e.g. a pending/scheduled state).
Decision: store `status: str` as Apollo's raw value, unvalidated against a
fixed set. Any interpretation of "what does this status mean" (e.g. "is
this terminal?") happens in application logic that explicitly handles
known values and treats anything else as unrecognized-but-preserved —
never as a validation failure.

### 8.3 Opens/clicks — separate `EmailMessageEvent` entity, not stored counts

Confirmed live (§2) that opens/clicks only exist as discrete,
individually-timestamped events returned by a separate
`/emailer_messages/{id}/activities` call — there is no flat count
anywhere in Apollo's API. Proposed relationship:

```
EmailSequence (1) ── (many) EmailMessage (1) ── (many) EmailMessageEvent
```

`EmailMessageEvent` is one row per individual Apollo event (one open, one
click, etc.) — not one row per message. `unique_opened`/`unique_clicked`
style counts are **computed** by counting event rows grouped by
`event_type`, per the existing "computed, not duplicated" convention
(`docs/ARCHITECTURE.md` §7.3) already used for `EmailSequence`'s
aggregate stats — they are never stored as columns on `EmailMessage`
itself, since that would duplicate data Apollo doesn't even provide as a
count.

Proposed (not implemented) shape:

```python
class EmailMessage(BaseModel):
    email_message_id: str
    apollo_message_id: str          # dedup key, see §8.5
    email_sequence_id: str
    email_sequence_step_id: str | None
    apollo_touch_id: str | None      # observed live (§1); semantics still UNKNOWN (§7)
    lead_id: str                     # resolved via contact_id -> Lead.apollo_contact_id

    status: str                      # Apollo's raw value, open string -- see §8.2
    failure_reason: str | None = None
    bounce: bool = False
    spam_blocked: bool = False

    replied: bool = False            # real field, but see §8.4 on reliability
    reply_class: str | None = None

    provider_message_id: str | None = None   # see §8.4
    provider_thread_id: str | None = None    # see §8.4

    created_at: datetime | None = None
    due_at: datetime | None = None
    completed_at: datetime | None = None
    failed_at: datetime | None = None

    last_synced_at: datetime


class EmailMessageEvent(BaseModel):
    email_message_event_id: str
    email_message_id: str
    apollo_event_id: str             # Apollo's own event `id` -- dedup key
    event_type: str                  # raw `event_group_type`/`type`, open string, same reasoning as §8.2
    occurred_at: datetime            # Apollo's per-event `created_at`
    apollo_contact_id: str | None = None
    readable_user_agent: str | None = None
    region: str | None = None        # Apollo's `state`
    country: str | None = None
```

No table, migration, or store exists for either of these yet.

### 8.4 Replies — explicitly unresolved, provider IDs preserved

Per §7, whether a reply reliably joins back to its originating
`EmailMessage` via `emailer_message_id` is unresolved — the one live
example available surfaced a structurally different, disconnected
record. No Inbox or reply-handling logic is being built now. Decision:
carry `provider_message_id`/`provider_thread_id` (the underlying Gmail
message/thread ids) on `EmailMessage` regardless, since they're real,
already-confirmed-live fields (§1) and are the most plausible bridge to a
future Gmail/Outlook-API-based Inbox — independent of whether Apollo's
own `replied` linkage ever gets resolved.

### 8.5 Sync — idempotent, resumable pagination, no scheduler

No background scheduler is being built. This describes the manual sync's
pagination strategy for whenever `EmailMessage` sync is implemented,
consistent with Phase 1's explicit-trigger-only pattern
(`EmailSequenceSyncService.sync()`).

Given §6's findings (no total-count envelope, no supported sort, offset
pagination with an unstable-under-writes default order):

- **Dedup key**: `apollo_message_id` (Apollo's `id` field) is the primary
  key for `EmailMessage` upserts; `apollo_event_id` (Apollo's event `id`)
  is the primary key for `EmailMessageEvent` upserts. Both are unique per
  Apollo, so re-fetching the same record twice must resolve to an update
  of the same row, never a duplicate — same pattern already used for
  `Lead.apollo_contact_id` and `EmailSequenceStep.apollo_step_id`.
- **Pagination loop**: since there's no total count, page forward with a
  fixed `per_page` until a page returns fewer rows than requested (or
  zero) — that's the only reliable end-of-results signal available (§6).
- **Per-campaign checkpoint, not global**: scope every sync call by
  `emailer_campaign_ids: [apollo_sequence_id]` (confirmed to actually
  filter, §6), one `EmailSequence` at a time, mirroring the existing
  per-campaign `asyncio.Lock` pattern already used for
  activate/pause/sync.
- **Checkpoint only advances after a page is fully persisted.** If a page
  fetch or persist fails partway through, the checkpoint (`last_synced_at`
  / last-completed-page marker) is **not** advanced — the next sync
  attempt re-fetches from the last confirmed-good point. This mirrors the
  existing rule already proven in Phase 1: a failed Apollo call must never
  advance `last_synced_at` (`test_failed_apollo_call_does_not_advance_last_synced_at`).
- **Safe to re-run from page 1 at any time.** Because upserts are keyed
  by Apollo's own ids, a full re-sync from the start is always safe and
  produces the same end state — this is the practical mitigation for the
  "unstable order under concurrent writes" risk noted in §6: even if a
  row is skipped or seen twice across runs, dedup means duplicates never
  persist, and a periodic full re-sync (still manually triggered, not a
  scheduler) would catch anything skipped.
- **No cursor/token exists to request** — Apollo does not expose one for
  this endpoint (confirmed live, §6); the "checkpoint" here is purely our
  own bookkeeping (which page/campaign combination last completed), not
  anything Apollo issues or validates.

---

## 9. Controlled `EmailMessage` test — blocked on sending infrastructure, not the app

A follow-up investigation attempted to unblock real `EmailMessage` data by
running a small, controlled test: three named contacts, one simple email
step, a brand-new sequence fully independent of Solstice Health. Two
checks were required before anything could be created or sent.

**Check 1 — contact resolution: PASSED (LIVE).** `POST /people/match`
resolved all three designated test contacts to real Apollo person
profiles:

| Email | Apollo person id | Name | Org |
|---|---|---|---|
| `johnadriancal@astronomic.com` | `66ec2c93dac1280001464242` | John Adrian | Astronomic |
| `lyzz@investordinners.com` | `6a6c9803fbbfac00184c504f` | Lyzz Culp | Investor Dinners |
| `victoria@astronomicmail.com` | `6a6c9805f55a2b00206d7bfc` | Victoria Bennett | (none) |

These three remain the designated controlled test set for whenever a
sending mailbox becomes available — no substitution needed, nothing
about this check blocks the test.

**Check 2 — mailbox health: FAILED (LIVE).** `GET /email_accounts`
returned **15 mailboxes total** in this Apollo account:

- **7 are outright revoked** (`revoked_at` set) — the same category as
  the Solstice Health mailbox (`65399fb6b7a25700a35bacd2`, revoked
  `2026-07-16T11:44:21`).
- **8 are not revoked, but every single one reports
  `inbox_placement_test_health_status: "unhealthy"`** — this is not a
  partial or mixed result; all 8 show the identical pattern:
  - **All 8 still mid-warmup**: 32–35% complete on a 45-day
    `mailwarming_vendor` schedule that started `2026-07-16` and runs
    through `2026-08-30`.
  - **All 8 have zero real send history**: `deliverability_score.sum_sent_count: 0` on every one, alongside `sum_hard_bounced_count: 0` and `sum_spam_blocked_count: 0` — these mailboxes have never actually sent anything, healthy or not.
  - `domain_health_score` is low (4–5) on every one.
  - Apollo is not hard-blocking sends from these (`domain_enforcement_action: "allow"`, `block_sending_for_unhealthy_domain: false`) — but its own deliverability signal explicitly flags none of them as ready.

**Conclusion: the controlled `EmailMessage` test is blocked by the
current state of this Apollo account's sending infrastructure, not by
anything in this application.** There is no code, schema, or service
defect causing this — `EmailSequenceSyncService`, the campaign lifecycle
actions, and the Apollo client all function correctly against real data
(proven in Phase 1 and in Solstice Health's real activation). The
blocker is that **every mailbox in this account is either revoked or
still in early warmup with no send history and a failed placement
test**, so there is currently no mailbox that meets a "confirmed
healthy" bar for a real send.

**Not done, per explicit instruction:** no test contact, list, sequence,
or enrollment was created; nothing was sent through any of the 15
mailboxes; Solstice Health and its revoked mailbox were not touched;
no other mailbox's configuration was modified. Only read-only lookups
(`/people/match`, `/email_accounts`) were made.

---

## Not done as part of this document

Per explicit instruction: no `EmailMessage` or `EmailMessageEvent` schema,
model, table, or migration was added or changed; no background scheduler
was built; the Solstice Health campaign was not rebuilt or re-enrolled;
the revoked Apollo mailbox was not touched; no fabricated/test
`EmailMessage` records were created; Phase 1 `EmailSequence`/
`EmailSequenceStep` code is unchanged.
