"""
Tests for the Lead system: a selected Apollo prospect becomes a durable
Lead exactly when CampaignService.build() confirms its Apollo contact
exists -- never merely for appearing in a search/ranking result, and
never when contact creation fails. Also covers Campaign lifecycle actions
(ready/activate/pause), which only ever persist local state after Apollo
itself confirms success.
"""

from unittest.mock import AsyncMock

import pytest

from app.models.campaign import Campaign, CampaignPlan, CampaignStatus, Filters
from app.models.lead import LeadStatus
from app.repositories.campaign_lead_store import MemoryCampaignLeadStore
from app.repositories.campaign_store import MemoryCampaignStore
from app.repositories.lead_store import MemoryLeadStore
from app.services.campaign_service import CampaignService
from app.services.lead_service import LeadService


def make_searched_campaign(campaign_id: str, prospects: list[dict]) -> Campaign:
    return Campaign(
        campaign_id=campaign_id,
        original_prompt=f"prompt for {campaign_id}",
        created_at="2026-07-31T00:00:00Z",
        status=CampaignStatus.SEARCHED,
        plan=CampaignPlan(campaign_name=f"Campaign {campaign_id}", filters=Filters(), sequence=[]),
        total_matches=len(prospects),
        selected_prospects=prospects,
    )


def make_built_campaign(campaign_id: str, apollo_sequence_id: str = "seq-1") -> Campaign:
    """A campaign already past build() -- for testing ready/activate/pause in isolation."""
    campaign = make_searched_campaign(campaign_id, [])
    campaign.status = CampaignStatus.BUILT
    campaign.apollo_list_id = "list-1"
    campaign.apollo_sequence_id = apollo_sequence_id
    return campaign


def make_prospect(person_id: str, **overrides) -> dict:
    base = {
        "id": person_id,
        "first_name": "Jamie",
        "last_name_obfuscated": "Sm***h",
        "title": "VP of Operations",
        "has_email": False,
        "organization": {"name": "Acme Logistics"},
        "claude_score": 90,
        "claude_reason": "Strong fit.",
    }
    base.update(overrides)
    return base


def make_mock_apollo(contact_id: str | None = None, create_contact_side_effect=None) -> AsyncMock:
    apollo = AsyncMock()
    apollo.create_list.return_value = {"label": {"id": "list-1"}}
    apollo.create_sequence.return_value = {"emailer_campaign": {"id": "seq-1"}}
    apollo.add_sequence_steps.return_value = {}
    apollo.list_email_accounts.return_value = {"email_accounts": [{"id": "mailbox-1"}]}
    apollo.enroll_contacts.return_value = {}
    if create_contact_side_effect is not None:
        apollo.create_contact.side_effect = create_contact_side_effect
    else:
        apollo.create_contact.return_value = {"contact": {"id": contact_id or "contact-1"}}
    return apollo


@pytest.fixture
def shared_lead_stores():
    """
    Simulates two CampaignService instances (as if from two separate
    campaign builds) sharing the same persistent Lead/CampaignLead layer --
    exactly like the real app, where one LeadService/store pair is
    constructed once at startup and shared across all campaigns.
    """
    lead_store = MemoryLeadStore()
    campaign_lead_store = MemoryCampaignLeadStore()
    campaign_store = MemoryCampaignStore()
    lead_service = LeadService(store=lead_store, campaign_lead_store=campaign_lead_store, campaign_store=campaign_store)
    return lead_store, campaign_lead_store, campaign_store, lead_service


def make_service(shared_lead_stores, apollo) -> CampaignService:
    _lead_store, campaign_lead_store, campaign_store, lead_service = shared_lead_stores
    return CampaignService(
        apollo=apollo,
        store=campaign_store,
        lead_service=lead_service,
        campaign_lead_store=campaign_lead_store,
    )


@pytest.mark.asyncio
async def test_build_creates_new_lead_from_selected_prospect(shared_lead_stores):
    lead_store, _cl_store, campaign_store, _ls = shared_lead_stores
    prospect = make_prospect("p1", first_name="Riley", title="VP of Sales")
    campaign = make_searched_campaign("c1", [prospect])
    await campaign_store.create(campaign)

    apollo = make_mock_apollo(contact_id="contact-1")
    service = make_service(shared_lead_stores, apollo)

    await service.build("c1")

    leads = await lead_store.list()
    assert len(leads) == 1
    lead = leads[0]
    assert lead.apollo_contact_id == "contact-1"
    assert lead.first_name == "Riley"
    assert lead.title == "VP of Sales"
    assert lead.company == "Acme Logistics"
    assert lead.status == LeadStatus.NEW
    # claude_score/reason are NOT on Lead -- they're per-campaign, on CampaignLead.
    assert not hasattr(lead, "claude_score")


@pytest.mark.asyncio
async def test_build_creates_campaign_lead_relationship_with_score(shared_lead_stores):
    lead_store, cl_store, campaign_store, _ls = shared_lead_stores
    campaign = make_searched_campaign(
        "c1", [make_prospect("p1", claude_score=88, claude_reason="Good title match.")]
    )
    await campaign_store.create(campaign)

    apollo = make_mock_apollo(contact_id="contact-1")
    service = make_service(shared_lead_stores, apollo)

    await service.build("c1")

    leads = await lead_store.list()
    memberships = await cl_store.list_for_campaign("c1")
    assert len(memberships) == 1
    assert memberships[0].lead_id == leads[0].lead_id
    assert memberships[0].campaign_id == "c1"
    assert memberships[0].claude_score == 88
    assert memberships[0].claude_reason == "Good title match."


@pytest.mark.asyncio
async def test_same_apollo_contact_in_two_campaigns_does_not_duplicate_lead(shared_lead_stores):
    lead_store, cl_store, campaign_store, _ls = shared_lead_stores
    campaign_a = make_searched_campaign("campaign-a", [make_prospect("p1")])
    campaign_b = make_searched_campaign("campaign-b", [make_prospect("p2")])
    await campaign_store.create(campaign_a)
    await campaign_store.create(campaign_b)

    # Both campaigns' builds resolve to the SAME Apollo contact id -- e.g.
    # Apollo's own dedup returning an existing contact for a second
    # campaign's create_contact call.
    apollo_a = make_mock_apollo(contact_id="shared-contact")
    apollo_b = make_mock_apollo(contact_id="shared-contact")

    service_a = make_service(shared_lead_stores, apollo_a)
    service_b = make_service(shared_lead_stores, apollo_b)

    await service_a.build("campaign-a")
    await service_b.build("campaign-b")

    leads = await lead_store.list()
    assert len(leads) == 1

    memberships = await cl_store.list_for_lead(leads[0].lead_id)
    assert {m.campaign_id for m in memberships} == {"campaign-a", "campaign-b"}


@pytest.mark.asyncio
async def test_each_campaign_lead_retains_its_own_score_and_reason(shared_lead_stores):
    """
    Requirement: the same Lead, selected into two different campaigns with
    two DIFFERENT Claude scores, must have each score preserved on its own
    CampaignLead row -- neither overwrites the other.
    """
    lead_store, cl_store, campaign_store, _ls = shared_lead_stores
    campaign_a = make_searched_campaign(
        "campaign-a", [make_prospect("p1", claude_score=95, claude_reason="Perfect fit for campaign A.")]
    )
    campaign_b = make_searched_campaign(
        "campaign-b", [make_prospect("p2", claude_score=60, claude_reason="Weaker fit for campaign B.")]
    )
    await campaign_store.create(campaign_a)
    await campaign_store.create(campaign_b)

    apollo_a = make_mock_apollo(contact_id="shared-contact")
    apollo_b = make_mock_apollo(contact_id="shared-contact")
    await make_service(shared_lead_stores, apollo_a).build("campaign-a")
    await make_service(shared_lead_stores, apollo_b).build("campaign-b")

    leads = await lead_store.list()
    assert len(leads) == 1
    memberships = {m.campaign_id: m for m in await cl_store.list_for_lead(leads[0].lead_id)}

    assert memberships["campaign-a"].claude_score == 95
    assert memberships["campaign-a"].claude_reason == "Perfect fit for campaign A."
    assert memberships["campaign-b"].claude_score == 60
    assert memberships["campaign-b"].claude_reason == "Weaker fit for campaign B."


@pytest.mark.asyncio
async def test_leads_global_data_not_overwritten_by_second_campaign(shared_lead_stores):
    """
    Requirement: a Lead's global fields (name, title, company) come from
    whichever campaign created it FIRST -- a second campaign selecting the
    same Apollo contact with different person details must not overwrite
    the Lead's original global data.
    """
    lead_store, _cl_store, campaign_store, _ls = shared_lead_stores
    campaign_a = make_searched_campaign(
        "campaign-a",
        [make_prospect("p1", first_name="Original", title="Original Title", organization={"name": "Original Co"})],
    )
    campaign_b = make_searched_campaign(
        "campaign-b",
        [make_prospect("p2", first_name="Different", title="Different Title", organization={"name": "Different Co"})],
    )
    await campaign_store.create(campaign_a)
    await campaign_store.create(campaign_b)

    apollo_a = make_mock_apollo(contact_id="shared-contact")
    apollo_b = make_mock_apollo(contact_id="shared-contact")
    await make_service(shared_lead_stores, apollo_a).build("campaign-a")
    await make_service(shared_lead_stores, apollo_b).build("campaign-b")

    leads = await lead_store.list()
    assert len(leads) == 1
    assert leads[0].first_name == "Original"
    assert leads[0].title == "Original Title"
    assert leads[0].company == "Original Co"


@pytest.mark.asyncio
async def test_rebuilding_campaign_does_not_duplicate_leads_or_campaign_leads(shared_lead_stores):
    lead_store, cl_store, campaign_store, _ls = shared_lead_stores
    campaign = make_searched_campaign("c1", [make_prospect("p1")])
    await campaign_store.create(campaign)

    apollo = make_mock_apollo(contact_id="contact-1")
    service = make_service(shared_lead_stores, apollo)

    await service.build("c1")
    first_leads = await lead_store.list()
    first_memberships = await cl_store.list_for_campaign("c1")

    await service.build("c1")
    second_leads = await lead_store.list()
    second_memberships = await cl_store.list_for_campaign("c1")

    assert len(first_leads) == len(second_leads) == 1
    assert len(first_memberships) == len(second_memberships) == 1
    # Apollo's create_contact must not have been called again for a
    # prospect that already has a confirmed contact id.
    assert apollo.create_contact.await_count == 1


@pytest.mark.asyncio
async def test_failed_contact_creation_does_not_create_orphan_lead(shared_lead_stores):
    lead_store, cl_store, campaign_store, _ls = shared_lead_stores
    campaign = make_searched_campaign("c1", [make_prospect("p1")])
    await campaign_store.create(campaign)

    apollo = make_mock_apollo(create_contact_side_effect=RuntimeError("Apollo is down"))
    service = make_service(shared_lead_stores, apollo)

    result = await service.build("c1")

    assert await lead_store.list() == []
    assert await cl_store.list_for_campaign("c1") == []
    assert any("Contact creation failed" in e for e in result.errors)
    # The failure is soft (contact-level, not list/sequence-level) --
    # existing behavior already lets the build proceed to completion.
    assert result.status == CampaignStatus.BUILT


@pytest.mark.asyncio
async def test_existing_build_behavior_unchanged_on_full_success(shared_lead_stores):
    """
    Baseline regression check: Campaign's own build-result fields behave
    exactly per the pre-existing contract when everything succeeds --
    unaffected by the Lead-system additions layered on top.
    """
    _lead_store, _cl_store, campaign_store, _ls = shared_lead_stores
    campaign = make_searched_campaign("c1", [make_prospect("p1")])
    await campaign_store.create(campaign)

    apollo = make_mock_apollo(contact_id="contact-1")
    service = make_service(shared_lead_stores, apollo)

    result = await service.build("c1")

    assert result.status == CampaignStatus.BUILT
    assert result.apollo_list_id == "list-1"
    assert result.apollo_sequence_id == "seq-1"
    assert result.contacts_created == 1
    assert result.apollo_contact_ids == ["contact-1"]
    assert result.contacts_enrolled == 1
    assert result.errors == []


@pytest.mark.asyncio
async def test_campaign_leads_view_returns_correct_score_and_reason(shared_lead_stores):
    """Campaign -> Leads composition (LeadService.list_for_campaign) surfaces the right per-campaign facts."""
    lead_store, cl_store, campaign_store, lead_service = shared_lead_stores
    campaign = make_searched_campaign(
        "c1", [make_prospect("p1", first_name="Riley", claude_score=77, claude_reason="Solid match.")]
    )
    await campaign_store.create(campaign)
    apollo = make_mock_apollo(contact_id="contact-1")
    await make_service(shared_lead_stores, apollo).build("c1")

    views = await lead_service.list_for_campaign("c1")

    assert len(views) == 1
    view = views[0]
    assert view.first_name == "Riley"
    assert view.claude_score == 77
    assert view.claude_reason == "Solid match."
    assert view.lead_status == LeadStatus.NEW


# --- Campaign lifecycle: ready / activate / pause ---


@pytest.mark.asyncio
async def test_mark_ready_requires_built_status(shared_lead_stores):
    _lead_store, _cl_store, campaign_store, _ls = shared_lead_stores
    campaign = make_searched_campaign("c1", [])  # status=SEARCHED, not BUILT
    await campaign_store.create(campaign)
    service = make_service(shared_lead_stores, make_mock_apollo())

    with pytest.raises(ValueError):
        await service.mark_ready("c1")


@pytest.mark.asyncio
async def test_mark_ready_is_idempotent_and_makes_no_apollo_call(shared_lead_stores):
    _lead_store, _cl_store, campaign_store, _ls = shared_lead_stores
    campaign = make_built_campaign("c1")
    await campaign_store.create(campaign)
    apollo = make_mock_apollo()
    service = make_service(shared_lead_stores, apollo)

    first = await service.mark_ready("c1")
    second = await service.mark_ready("c1")

    assert first.status == second.status == CampaignStatus.READY
    apollo.activate_sequence.assert_not_called()
    apollo.deactivate_sequence.assert_not_called()


@pytest.mark.asyncio
async def test_activate_only_changes_local_state_after_apollo_succeeds(shared_lead_stores):
    _lead_store, _cl_store, campaign_store, _ls = shared_lead_stores
    campaign = make_built_campaign("c1")
    campaign.status = CampaignStatus.READY
    await campaign_store.create(campaign)
    apollo = make_mock_apollo()
    apollo.activate_sequence.return_value = {}
    service = make_service(shared_lead_stores, apollo)

    result = await service.activate("c1")

    apollo.activate_sequence.assert_awaited_once_with("seq-1")
    assert result.status == CampaignStatus.ACTIVE
    assert result.activated is True
    stored = await campaign_store.get("c1")
    assert stored.status == CampaignStatus.ACTIVE
    assert stored.activated is True


@pytest.mark.asyncio
async def test_failed_activation_does_not_change_local_state(shared_lead_stores):
    _lead_store, _cl_store, campaign_store, _ls = shared_lead_stores
    campaign = make_built_campaign("c1")
    campaign.status = CampaignStatus.READY
    await campaign_store.create(campaign)
    apollo = make_mock_apollo()
    apollo.activate_sequence.side_effect = RuntimeError("Apollo rejected activation")
    service = make_service(shared_lead_stores, apollo)

    with pytest.raises(RuntimeError):
        await service.activate("c1")

    stored = await campaign_store.get("c1")
    assert stored.status == CampaignStatus.READY  # unchanged
    assert stored.activated is False  # unchanged


@pytest.mark.asyncio
async def test_activate_is_idempotent_when_already_active(shared_lead_stores):
    _lead_store, _cl_store, campaign_store, _ls = shared_lead_stores
    campaign = make_built_campaign("c1")
    campaign.status = CampaignStatus.ACTIVE
    campaign.activated = True
    await campaign_store.create(campaign)
    apollo = make_mock_apollo()
    service = make_service(shared_lead_stores, apollo)

    result = await service.activate("c1")

    assert result.status == CampaignStatus.ACTIVE
    apollo.activate_sequence.assert_not_called()  # no Apollo call for an already-active campaign


@pytest.mark.asyncio
async def test_pause_only_changes_local_state_after_apollo_succeeds(shared_lead_stores):
    _lead_store, _cl_store, campaign_store, _ls = shared_lead_stores
    campaign = make_built_campaign("c1")
    campaign.status = CampaignStatus.ACTIVE
    campaign.activated = True
    await campaign_store.create(campaign)
    apollo = make_mock_apollo()
    apollo.deactivate_sequence.return_value = {}
    service = make_service(shared_lead_stores, apollo)

    result = await service.pause("c1")

    apollo.deactivate_sequence.assert_awaited_once_with("seq-1")
    assert result.status == CampaignStatus.PAUSED
    assert result.activated is False
    stored = await campaign_store.get("c1")
    assert stored.status == CampaignStatus.PAUSED
    assert stored.activated is False


@pytest.mark.asyncio
async def test_failed_pause_does_not_change_local_state(shared_lead_stores):
    _lead_store, _cl_store, campaign_store, _ls = shared_lead_stores
    campaign = make_built_campaign("c1")
    campaign.status = CampaignStatus.ACTIVE
    campaign.activated = True
    await campaign_store.create(campaign)
    apollo = make_mock_apollo()
    apollo.deactivate_sequence.side_effect = RuntimeError("Apollo rejected deactivation")
    service = make_service(shared_lead_stores, apollo)

    with pytest.raises(RuntimeError):
        await service.pause("c1")

    stored = await campaign_store.get("c1")
    assert stored.status == CampaignStatus.ACTIVE  # unchanged
    assert stored.activated is True  # unchanged


@pytest.mark.asyncio
async def test_pause_is_idempotent_when_already_paused(shared_lead_stores):
    _lead_store, _cl_store, campaign_store, _ls = shared_lead_stores
    campaign = make_built_campaign("c1")
    campaign.status = CampaignStatus.PAUSED
    await campaign_store.create(campaign)
    apollo = make_mock_apollo()
    service = make_service(shared_lead_stores, apollo)

    result = await service.pause("c1")

    assert result.status == CampaignStatus.PAUSED
    apollo.deactivate_sequence.assert_not_called()


@pytest.mark.asyncio
async def test_activate_rejects_campaign_not_yet_ready(shared_lead_stores):
    _lead_store, _cl_store, campaign_store, _ls = shared_lead_stores
    campaign = make_built_campaign("c1")  # status=BUILT, not READY
    await campaign_store.create(campaign)
    apollo = make_mock_apollo()
    service = make_service(shared_lead_stores, apollo)

    with pytest.raises(ValueError):
        await service.activate("c1")

    apollo.activate_sequence.assert_not_called()
