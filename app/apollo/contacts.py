"""
Apollo contact creation.

List membership is set here via `label_names`, not through a separate
"add to list" call -- Apollo has no such endpoint. `label_names` on create
establishes membership directly; on *update* it replaces membership
entirely (docs: "Passing new values will overwrite existing lists"), so
this is only safe to rely on at creation time.
"""

from app.apollo.client import ApolloBaseClient


class ContactsClient(ApolloBaseClient):
    async def create_contact(self, person: dict) -> dict:
        payload = {
            k: v
            for k, v in {
                "first_name": person.get("first_name"),
                "last_name": person.get("last_name"),
                "email": person.get("email"),
                "organization_name": person.get("organization_name"),
                "title": person.get("title"),
                "label_names": person.get("label_names"),
            }.items()
            if v
        }
        return await self.request("POST", "/contacts", json=payload)

    async def update_contact_custom_field(self, contact_id: str, field_id: str, value: str) -> dict:
        return await self.request(
            "PUT",
            f"/contacts/{contact_id}",
            json={"typed_custom_fields": {field_id: value}},
        )
