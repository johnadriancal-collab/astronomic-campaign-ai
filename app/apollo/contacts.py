"""
Apollo contact creation.
"""

from app.apollo.client import ApolloBaseClient


class ContactsClient(ApolloBaseClient):
    async def create_contact(self, person: dict) -> dict:
        payload = {
            "first_name": person.get("first_name"),
            "last_name": person.get("last_name"),
            "email": person.get("email"),
            "organization_name": person.get("organization_name"),
            "title": person.get("title"),
        }
        return await self.request("POST", "/contacts", json=payload)

    async def update_contact_custom_field(self, contact_id: str, field_id: str, value: str) -> dict:
        return await self.request(
            "PUT",
            f"/contacts/{contact_id}",
            json={"typed_custom_fields": {field_id: value}},
        )
