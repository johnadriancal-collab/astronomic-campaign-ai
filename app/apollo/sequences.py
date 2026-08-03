"""
Apollo sequence ("emailer campaign") management.

Apollo's naming is inconsistent across this resource, in two different ways:

1. Creating/updating a sequence uses the current `/sequences` path, while
   sequence actions (steps, enrollment, activate/deactivate) still live
   under the legacy `/emailer_campaigns` path. Both refer to the same
   underlying object -- a `/sequences` create call still returns its result
   wrapped in an `emailer_campaign` key.
2. The human-readable doc titles don't match their own URL paths: "Activate
   a Sequence" is `/approve`, and "Deactivate a Sequence" is `/abort` --
   neither `/activate` nor `/deactivate`/`/pause` exist.

Verify each path independently against current docs; don't assume naming is
consistent anywhere in this resource.
"""

from app.apollo.client import ApolloBaseClient


class SequencesClient(ApolloBaseClient):
    async def list_sequences(self, page: int = 1, per_page: int = 100) -> dict:
        """
        Full-account listing for CampaignSyncService's discovery pass.
        Confirmed LIVE: unlike /emailer_messages/search, this endpoint
        returns a real `pagination: {page, per_page, total_entries,
        total_pages}` envelope, so callers can page reliably using
        `total_pages` instead of a short-page heuristic.

        CONFIRMED LIVE AND IMPORTANT: this endpoint only ever returns
        non-archived sequences, and -- despite being named "search" --
        passing `emailer_campaign_ids` (or `archived`/`include_archived`/
        `status`/`emailer_campaign_statuses`) has NO effect on the result
        set. Tested directly: filtering by a real id, a different real id,
        and a nonexistent id all returned the identical unfiltered list.
        Do not add filter params to this call assuming they'll narrow
        anything -- verify against the live API first if that's ever
        needed again.
        """
        return await self.request(
            "POST", "/emailer_campaigns/search", json={"page": page, "per_page": per_page}
        )

    async def create_sequence(self, name: str) -> dict:
        return await self.request("POST", "/sequences", json={"name": name})

    async def update_sequence(self, sequence_id: str, updates: dict) -> dict:
        return await self.request("PUT", f"/sequences/{sequence_id}", json=updates)

    async def add_sequence_steps(self, sequence_id: str, steps: list[dict]) -> dict:
        """
        steps: ordered list of {"day": int, "subject": str, "body": str}.

        Apollo has no standalone "create step" endpoint -- there is no
        /emailer_campaigns/{id}/steps route. Steps are set via a single PUT
        to /sequences/{id} (see update_sequence) carrying a full
        `emailer_steps` array; new steps/touches are created by omitting
        their `id`. `day` here is the plan's cumulative offset (0, 3, 6, 9),
        but Apollo's `wait_time` is the delta from the previous step, so
        this converts cumulative -> per-step deltas.
        """
        emailer_steps = []
        previous_day = 0
        for i, step in enumerate(steps):
            wait_time = step["day"] - previous_day if i > 0 else step["day"]
            previous_day = step["day"]
            emailer_steps.append(
                {
                    "position": i + 1,
                    "type": "auto_email",
                    "wait_mode": "day",
                    "wait_time": wait_time,
                    "emailer_touches": [
                        {
                            "emailer_template": {
                                "subject": step["subject"],
                                # body_html, not body -- convert plain-text
                                # newlines so paragraph breaks survive as HTML.
                                "body_html": step["body"].replace("\n", "<br>"),
                            }
                        }
                    ],
                }
            )

        return await self.update_sequence(sequence_id, {"emailer_steps": emailer_steps})

    async def enroll_contacts(
        self,
        sequence_id: str,
        contact_ids: list[str],
        mailbox_id: str,
        allow_no_email: bool = True,
    ) -> dict:
        """
        mailbox_id (send_email_from_email_account_id) is a REQUIRED field for
        this endpoint, not optional -- omitting it is what caused the 422s.
        There is no way to enroll contacts without a real connected mailbox.

        allow_no_email (sequence_no_email) defaults to True because contacts
        created from search_people() never have a real email (Apollo's free
        search endpoint withholds it) -- without this flag Apollo silently
        skips every one of them (`skipped_contact_ids: {"...": "contacts_
        without_email"}`) instead of erroring, which looks like success but
        enrolls nobody. Set False if the contacts are known to have emails.
        """
        payload = {
            "contact_ids": contact_ids,
            "emailer_campaign_id": sequence_id,
            "send_email_from_email_account_id": mailbox_id,
            "sequence_no_email": allow_no_email,
        }
        return await self.request(
            "POST", f"/emailer_campaigns/{sequence_id}/add_contact_ids", json=payload
        )

    async def activate_sequence(self, sequence_id: str) -> dict:
        return await self.request("POST", f"/emailer_campaigns/{sequence_id}/approve", json={})

    async def deactivate_sequence(self, sequence_id: str) -> dict:
        return await self.request("POST", f"/emailer_campaigns/{sequence_id}/abort", json={})

    async def get_sequence(self, sequence_id: str) -> dict:
        """
        Direct single-sequence lookup -- the correct way to fetch one known
        sequence by id (list_sequences' filter params don't work, see
        above). Confirmed LIVE: works for both active and archived
        sequences (archived ones just come back with `archived: true`
        rather than a 404), which is exactly what reconciliation needs.

        Response is wrapped: `{"emailer_campaign": {..., "emailer_steps": [...]}, ...}`
        -- unwrap `resp["emailer_campaign"]` to get the same shape as one
        element of list_sequences()'s `emailer_campaigns` array.
        """
        return await self.request("GET", f"/emailer_campaigns/{sequence_id}")
