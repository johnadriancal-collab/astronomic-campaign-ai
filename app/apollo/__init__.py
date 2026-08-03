"""
Unified Apollo API client, combining people/contact/list/sequence methods
into a single facade so the rest of the app can just do:

    from app.apollo import ApolloClient
    apollo = ApolloClient()
    await apollo.search_people(...)
    await apollo.create_sequence(...)
"""

from app.apollo.contacts import ContactsClient
from app.apollo.email_accounts import EmailAccountsClient
from app.apollo.lists import ListsClient
from app.apollo.messages import MessagesClient
from app.apollo.people import PeopleClient
from app.apollo.sequences import SequencesClient


class ApolloClient(
    PeopleClient, ContactsClient, ListsClient, SequencesClient, EmailAccountsClient, MessagesClient
):
    """All Apollo method groups share the same base HTTP client/config,
    so combining them via inheritance avoids re-instantiating separate
    clients or duplicating auth/retry plumbing."""

    pass
