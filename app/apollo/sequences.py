"""
Apollo sequence ("emailer campaign") management.

Apollo's naming is inconsistent across this resource: creating/updating a
sequence uses the current `/sequences` path, while sequence actions (steps,
enrollment, activate/pause) still live under the legacy `/emailer_campaigns`
path. Both refer to the same underlying object -- a `/sequences` create call
still returns its result wrapped in an `emailer_campaign` key. Verify each
path independently against current docs; don't assume they've all moved
together.
"""

from app.apollo.client import ApolloBaseClient


class SequencesClient(ApolloBaseClient):
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
        self, sequence_id: str, contact_ids: list[str], mailbox_id: str | None = None
    ) -> dict:
        payload: dict = {
            "contact_ids": contact_ids,
            "emailer_campaign_id": sequence_id,
        }
        if mailbox_id:
            payload["send_email_from_email_account_id"] = mailbox_id
        return await self.request(
            "POST", f"/emailer_campaigns/{sequence_id}/add_contact_ids", json=payload
        )

    async def activate_sequence(self, sequence_id: str) -> dict:
        return await self.request("POST", f"/emailer_campaigns/{sequence_id}/activate", json={})

    async def pause_sequence(self, sequence_id: str) -> dict:
        return await self.request("POST", f"/emailer_campaigns/{sequence_id}/pause", json={})

    async def get_sequence(self, sequence_id: str) -> dict:
        return await self.request("GET", f"/emailer_campaigns/{sequence_id}")
