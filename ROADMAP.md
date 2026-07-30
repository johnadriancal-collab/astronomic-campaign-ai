# Campaign AI — Implementation Roadmap

Tracks which pieces of the Claude ↔ Apollo pipeline have been implemented
and verified against the **real** Apollo API (not just docs review), so
work can continue incrementally without re-deriving status from scratch.

## Completed (verified live against real Anthropic + Apollo accounts)

| Feature | Endpoint(s) | Notes |
|---|---|---|
| Campaign plan generation | Anthropic Messages API | `/campaign/preview` returns a valid `CampaignPlan` (filters + 4-email sequence) from a plain-English prompt. |
| Apollo people search | `POST /mixed_people/api_search` | Path fixed from invalid `/mixed_people/search`. `industries` filter mapped to `q_organization_keyword_tags` (no true industry taxonomy exists). `funding_stage` filter dropped entirely — no categorical equivalent in Apollo's current API. |
| Apollo sequence creation | `POST /sequences` | Path fixed from legacy/incorrect `/emailer_campaigns`. Response wraps result in `emailer_campaign` key. |
| Apollo list creation | `POST /labels` | Path fixed from invented `/contact_lists`. Requires `modality` (`"contacts"`/`"accounts"`), not present in original code. |
| Apollo email step creation | `PUT /sequences/{id}` (via `update_sequence`) | No standalone "create step" endpoint exists. Steps are set by sending a full `emailer_steps` array; cumulative day-offsets (0/3/6/9) converted to Apollo's per-step `wait_time` deltas (0/3/3/3). Verified live on `AI Test Sequence` (id `6a69d61cf14d6e000ceb9c15`) — re-confirmed via read-only `GET` on 2026-07-30: 4 steps present, subjects/bodies/delays intact, still inactive. |

## Explicitly paused

| Feature | Status |
|---|---|
| List membership (adding contacts to a list) | Paused by decision, not researched yet. Current code (`add_people_to_list`) still calls invented `/contact_lists/{id}/add_contact_ids` — almost certainly wrong, same issue as list creation before it was fixed. |

## Not started

- **Contact creation** (`create_contact` in `app/apollo/contacts.py`) — fields were checked against docs during the initial project review but never exercised against the real API this session.
- **Sequence enrollment** (adding contacts to a sequence so it has something to send to).
- **Sequence activation / launch** (the actual "go live" action — highest-stakes, last in line).
- **Orchestration wiring** (`app/services/campaign_service.py`) — still calls the old, now-removed `add_sequence_step` (singular) and parses list-creation responses expecting `contact_list`/`id` keys instead of the real `label` key. Needs updating once enough of the underlying pieces are fixed to make it worth touching.

## Recommended next step: Contact creation

Both remaining pieces of the write pipeline — **list membership** (paused) and **sequence enrollment** (not started) — need real Apollo `contact_id`s as input. Right now the only thing this app can produce is Apollo *people* (`search_people` results), which are not contacts — Apollo's search endpoint deliberately withholds real names/emails until a person is explicitly saved as a contact. Contact creation is the shared dependency both paused/upcoming features are blocked on, and it's also the one piece of Apollo API code in this project that has never been checked against the live API. Recommend implementing and verifying that next, before returning to list membership or starting enrollment.
