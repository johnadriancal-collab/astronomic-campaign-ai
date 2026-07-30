"""
Apollo prospect list management.

`contact_lists` is not a real Apollo resource. Apollo's "Lists" feature is
exposed via the `labels` resource -- creation requires both `name` and
`modality` ("contacts" or "accounts"); names must be unique per modality
within the team (a duplicate name returns a 422).
"""

from app.apollo.client import ApolloBaseClient


class ListsClient(ApolloBaseClient):
    async def create_list(self, name: str, modality: str = "contacts") -> dict:
        return await self.request(
            "POST", "/labels", json={"name": name, "modality": modality}
        )

    async def add_people_to_list(self, list_id: str, contact_ids: list[str]) -> dict:
        return await self.request(
            "POST",
            f"/contact_lists/{list_id}/add_contact_ids",
            json={"contact_ids": contact_ids},
        )
