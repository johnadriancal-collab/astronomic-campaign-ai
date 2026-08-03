"""
Apollo per-message and per-event data -- confirmed LIVE against real
messages during the research documented in
docs/APOLLO_MESSAGE_API_FINDINGS.md (#1, #2, #6). Two endpoints:

1. `/emailer_messages/search` -- paginated list of messages for a
   sequence. Confirmed live: standard page/per_page params, no
   `pagination`/total-count envelope, no supported sort param (sorting by
   `created_at` 422s). This means the sync loop must page forward until a
   short/empty page, never read a total up front.
2. `/emailer_messages/{id}/activities` -- per-message open/click events,
   grouped by `event_group_type`, each event individually timestamped.
   No bulk version of this exists across a whole sequence -- it's one call
   per message, which is why EmailMessageSyncService keeps event sync
   separate from message sync rather than bundling it automatically.
"""

from app.apollo.client import ApolloBaseClient


class MessagesClient(ApolloBaseClient):
    async def search_messages(self, apollo_sequence_id: str, page: int = 1, per_page: int = 100) -> dict:
        return await self.request(
            "POST",
            "/emailer_messages/search",
            json={"emailer_campaign_ids": [apollo_sequence_id], "page": page, "per_page": per_page},
        )

    async def get_message_activities(self, apollo_message_id: str) -> dict:
        return await self.request("GET", f"/emailer_messages/{apollo_message_id}/activities")
