"""
Guards the Apollo infrastructure this app still needs for enrichment/
contact-research use even with Campaign Manager's Apollo Campaign/Sequence
integration disabled: these modules must stay importable, and the specific
methods used for enrichment must still exist with their expected shape.
Not a behavioral test against the live Apollo API -- just a regression
tripwire so a future cleanup pass on the campaign side can't accidentally
take enrichment down with it.
"""

import inspect

from app.apollo import ApolloClient
from app.apollo.client import ApolloBaseClient
from app.apollo.contacts import ContactsClient
from app.apollo.lists import ListsClient
from app.apollo.people import PeopleClient
from app.config import settings


def test_apollo_client_classes_are_importable():
    assert ApolloClient is not None
    assert ApolloBaseClient is not None
    assert PeopleClient is not None
    assert ContactsClient is not None
    assert ListsClient is not None


def test_apollo_client_combines_all_method_groups():
    client = ApolloClient()
    assert isinstance(client, PeopleClient)
    assert isinstance(client, ContactsClient)
    assert isinstance(client, ListsClient)


def test_people_client_enrichment_methods_present():
    assert inspect.iscoroutinefunction(PeopleClient.search_people)
    assert inspect.iscoroutinefunction(PeopleClient.search_companies)


def test_contacts_client_enrichment_methods_present():
    assert inspect.iscoroutinefunction(ContactsClient.create_contact)
    assert inspect.iscoroutinefunction(ContactsClient.update_contact_custom_field)


def test_apollo_settings_remain_configured():
    assert isinstance(settings.apollo_api_key, str) and settings.apollo_api_key
    assert isinstance(settings.apollo_base_url, str) and settings.apollo_base_url
