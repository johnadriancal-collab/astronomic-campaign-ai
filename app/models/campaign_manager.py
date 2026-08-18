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
