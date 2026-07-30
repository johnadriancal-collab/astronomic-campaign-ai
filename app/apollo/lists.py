"""
Apollo prospect list management.

`contact_lists` is not a real Apollo resource. Apollo's "Lists" feature is
exposed via the `labels` resource -- creation requires both `name` and
`modality` ("contacts" or "accounts"); names must be unique per modality
within the team (a duplicate name returns a 422).

There is no "add existing contacts to a list" endpoint -- Apollo has no
POST /labels/{id}/add_contact_ids or similar. List membership is set via
`label_names` on contact creation instead (see contacts.py::create_contact).
This class only handles list creation; the list must exist before contacts
reference its name.
"""

from app.apollo.client import ApolloBaseClient


class ListsClient(ApolloBaseClient):
    async def create_list(self, name: str, modality: str = "contacts") -> dict:
        return await self.request(
            "POST", "/labels", json={"name": name, "modality": modality}
        )
