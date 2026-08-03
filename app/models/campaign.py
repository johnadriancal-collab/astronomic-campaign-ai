"""
Pydantic models for the campaign generation + execution pipeline.

`Campaign` is the single source of truth for one campaign's entire
lifecycle (see CampaignStore/CampaignService). `CampaignPlan` is set on it
exactly once, at creation, and never reassigned -- every stage after that
reads the same stored plan instead of asking Claude again.

Metrics are tracked as distinct, non-conflatable fields on purpose: how
many prospects exist in Apollo for these filters (`total_matches`) is a
different fact from how many were retrieved for ranking
(`retrieval_pool_size`), which is different again from how many Claude
actually picked (`selected_prospect_count`), which is different again from
how many contacts/enrollments actually happened in Apollo
(`contacts_created`/`contacts_enrolled`). Nothing in this model should ever
let "160 matched" be confused with "25 were built."
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, computed_field


class Filters(BaseModel):
    locations: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    titles: list[str] = Field(default_factory=list)
    company_size: list[str] = Field(default_factory=list)
    funding_stage: list[str] = Field(default_factory=list)


class SequenceStep(BaseModel):
    day: int
    subject: str
    body: str


class CampaignPlan(BaseModel):
    """
    The structured plan Claude returns. `launch` reflects what Claude
    *suggested*, but does not by itself trigger sending — see
    CampaignService.build for why.
    """

    campaign_name: str
    filters: Filters
    sequence: list[SequenceStep]
    launch: bool = False


class CampaignStatus(str, Enum):
    DRAFT = "draft"  # plan generated, not yet searched
    SEARCHED = "searched"  # prospects retrieved + ranked
    BUILDING = "building"  # build in progress
    BUILT = "built"  # list/sequence/contacts/enrollment all done
    FAILED = "failed"  # build attempted, errored partway -- retry resumes from here
    READY = "ready"  # a human has explicitly confirmed this is ready to activate --
    # app-side only, never set automatically anywhere; the deliberate approval
    # gate between "mechanically built" and "actually goes live in Apollo"
    ACTIVE = "active"  # Apollo confirmed activate_sequence succeeded -- never set on request alone
    PAUSED = "paused"  # Apollo confirmed deactivate_sequence succeeded -- never set on request alone
    # No COMPLETED yet -- there's no Apollo signal we sync today that could
    # honestly drive it (see docs/ARCHITECTURE.md's open sync-strategy question).


class Campaign(BaseModel):
    campaign_id: str
    original_prompt: str
    created_at: datetime
    status: CampaignStatus = CampaignStatus.DRAFT

    plan: CampaignPlan

    # Search stage. desired_prospect_count is the target requested at
    # preview time; selected_prospect_count (below) is what was actually
    # achieved -- normally equal, but not guaranteed if Apollo/Claude
    # return fewer candidates than requested.
    desired_prospect_count: int = 25
    retrieval_pool_size: int = 100
    total_matches: int | None = None
    selected_prospects: list[dict] = Field(default_factory=list)

    # Build stage. apollo_contact_map is canonical -- keyed by the Apollo
    # person id from selected_prospects, mapping to the Apollo contact id
    # created for them. This per-person correspondence (not just a flat
    # list) is what makes contact creation resumable per-prospect on retry
    # (a previous version tracked only a flat list with no such mapping --
    # a retry after a partial failure would recreate every contact, not
    # just the failed ones) and is also what lets the Lead system know
    # exactly which prospect produced which Apollo contact (see
    # CampaignService.build() and app/services/lead_service.py).
    # apollo_contact_ids/contacts_created are derived from this map rather
    # than tracked separately, so they can never drift apart from it.
    # contacts_enrolled is NOT derivable the same way -- Apollo's enroll
    # call can accept fewer than were sent, so it's a real,
    # independently-tracked outcome, not a count of an input list.
    apollo_list_id: str | None = None
    apollo_sequence_id: str | None = None
    apollo_contact_map: dict[str, str] = Field(default_factory=dict)
    contacts_enrolled: int = 0
    activated: bool = False

    logs: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def selected_prospect_count(self) -> int:
        return len(self.selected_prospects)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def apollo_contact_ids(self) -> list[str]:
        return list(self.apollo_contact_map.values())

    @computed_field  # type: ignore[prop-decorator]
    @property
    def contacts_created(self) -> int:
        return len(self.apollo_contact_map)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def build_report(self) -> dict:
        """
        Derived, not stored -- a convenience bundle of facts that already
        live on the fields above. Exists so API consumers have one place to
        read "how did the build go" without hand-picking fields, but it is
        NOT a second source of truth: every value here is computed fresh
        from the canonical fields, so it can never disagree with them.
        """
        return {
            "apollo_list_id": self.apollo_list_id,
            "apollo_sequence_id": self.apollo_sequence_id,
            "contacts_created": self.contacts_created,
            "contacts_enrolled": self.contacts_enrolled,
            "activated": self.activated,
            "errors": self.errors,
        }


class PreviewRequest(BaseModel):
    prompt: str
    desired_prospect_count: int = 25


class SearchRequest(BaseModel):
    campaign_id: str


class BuildRequest(BaseModel):
    campaign_id: str
