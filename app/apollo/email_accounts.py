"""
Apollo connected email accounts.

Needed because sequence enrollment (see sequences.py::enroll_contacts)
requires a real `send_email_from_email_account_id` -- there is no way to
enroll contacts without one. This lets the service look one up when
DEFAULT_SENDER_MAILBOX_ID isn't configured, instead of guessing.
"""

from app.apollo.client import ApolloBaseClient


class EmailAccountsClient(ApolloBaseClient):
    async def list_email_accounts(self) -> dict:
        return await self.request("GET", "/email_accounts")
