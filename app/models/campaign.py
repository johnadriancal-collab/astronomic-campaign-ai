"""
Pydantic models for the campaign generation + execution pipeline.
"""

from pydantic import BaseModel, Field


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
    CampaignService.build_campaign for why.
    """

    campaign_name: str
    filters: Filters
    sequence: list[SequenceStep]
    launch: bool = False


class CampaignRequest(BaseModel):
    prompt: str


class CampaignExecutionReport(BaseModel):
    campaign_name: str
    apollo_list_id: str | None = None
    apollo_sequence_id: str | None = None
    prospects_found: int = 0
    prospects_enrolled: int = 0
    activated: bool = False
    errors: list[str] = Field(default_factory=list)
