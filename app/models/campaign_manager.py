"""
Campaign Manager Integration Phase -- read-side aggregation DTO ONLY.

This module deliberately owns no persistence. Campaign (Apollo) and
MailCampaign (Astronomic Mail) remain the sole authoritative models for
their respective systems -- see app/models/campaign.py and app/models/mail.py.
UnifiedCampaignSummary exists purely so the Campaign Manager dashboard can
render both in one list without either backend knowing the other exists.

CampaignStatusBucket is a presentation-only vocabulary. It is never written
back to Campaign.status or MailCampaign.status, and it never replaces either
enum -- app/api/campaign_manager.py maps the real, authoritative status into
this bucket at read time, every time, from scratch.
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class SendingMethod(str, Enum):
    APOLLO = "apollo"
    ASTRONOMIC_MAIL = "astronomic_mail"


class CampaignStatusBucket(str, Enum):
    DRAFT = "draft"
    IN_PROGRESS = "in_progress"
    READY = "ready"
    ACTIVE = "active"
    PAUSED = "paused"
    FAILED = "failed"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class UnifiedCampaignSummary(BaseModel):
    id: str
    sending_method: SendingMethod
    name: str
    status_bucket: CampaignStatusBucket
    raw_status: str
    summary: str
    created_at: datetime
    detail_path: str


# Presentation-only bucketing -- never written back to either store, and
# recomputed from the real enum value on every read. Lives here (a model
# file, importable by both the API layer and the service layer) rather
# than in app/api/campaign_manager.py, specifically so
# app/services/astro_campaign_tools.py can reuse the exact same mapping
# without a service importing from the API layer (this app's services
# never import app.api.*).
APOLLO_STATUS_BUCKET: dict[str, CampaignStatusBucket] = {
    "draft": CampaignStatusBucket.DRAFT,
    "searched": CampaignStatusBucket.IN_PROGRESS,
    "building": CampaignStatusBucket.IN_PROGRESS,
    "built": CampaignStatusBucket.IN_PROGRESS,
    "failed": CampaignStatusBucket.FAILED,
    "ready": CampaignStatusBucket.READY,
    "active": CampaignStatusBucket.ACTIVE,
    "paused": CampaignStatusBucket.PAUSED,
}

MAIL_STATUS_BUCKET: dict[str, CampaignStatusBucket] = {
    "draft": CampaignStatusBucket.DRAFT,
    "ready": CampaignStatusBucket.READY,
    "active": CampaignStatusBucket.ACTIVE,
    "paused": CampaignStatusBucket.PAUSED,
    # Deliberately its own bucket, distinct from ARCHIVED: COMPLETED means
    # the campaign successfully finished execution; ARCHIVED means it was
    # intentionally archived/retired. Conflating the two here would
    # reintroduce, in this dashboard, the exact ambiguity the campaign
    # detail page's locked-status banner was fixed to stop showing (see
    # frontend/lib/mail.ts's campaignLockedBannerTitle()).
    "completed": CampaignStatusBucket.COMPLETED,
    "archived": CampaignStatusBucket.ARCHIVED,
}
