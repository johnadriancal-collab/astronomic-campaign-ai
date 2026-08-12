"""
Models for the Email -> CRM Intake with Human Approval pipeline (Phase 1).

An email sent to the CRM's intake address is turned into exactly one
EmailIntakeItem (idempotent on gmail_message_id -- see
SQLiteEmailIntakeStore). Ingestion may match an existing CRM contact
(reusing CrmService.classify_match() verbatim -- no second dedup engine)
and may propose field changes (EmailCrmFieldChange, via a swappable
EmailExtractor -- see app/services/email_intake_extraction.py), but it
NEVER writes to the CRM. A CRM write happens only through
EmailIntakeService.approve(), which re-fetches the live contact, checks
each approved field's current value against the snapshot this proposal
was generated against, and only then calls CrmService.update_contact()
-- the same manual-edit path a human using the contact edit page already
goes through. See EmailIntakeService's module docstring for the full
approve/reject/stale-detection contract.

Nothing here stores Gmail credentials, tokens, or OAuth state -- only the
plain message content a webhook caller (a future Apps Script bridge, or a
synthetic test payload in Phase 1) already chose to send.
"""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class EmailIntakeStatus(str, Enum):
    PENDING_REVIEW = "pending_review"  # matched (or no match needed yet) -- proposal (possibly empty) ready for review
    NEEDS_MATCH = "needs_match"  # ambiguous (possible_duplicate) or no contact found -- human must pick one, or leave unmatched
    APPROVED = "approved"
    REJECTED = "rejected"
    ERROR = "error"  # extraction/ingestion itself raised -- never used just because extraction found nothing


class EmailFieldChangeOperation(str, Enum):
    SET = "set"  # scalar overwrite -- current_value -> proposed_value directly
    UNION_ADD = "union_add"  # proposed_value is the FULL post-merge list (already computed at extraction time)
    APPEND = "append"  # proposed_value is current text + separator + new text (already computed)


class EmailAttachmentMeta(BaseModel):
    """
    Metadata only -- Phase 1 never reads attachment content. filename/
    content_type come directly from the webhook payload (or, in Phase 2,
    from Gmail's own attachment listing); size_bytes is optional since not
    every caller can supply it.
    """

    filename: str
    content_type: str | None = None
    size_bytes: int | None = None


class EmailCrmFieldChange(BaseModel):
    """
    One proposed change to one CRM field on the matched contact.
    `current_value` is a SNAPSHOT taken at proposal-generation time --
    EmailIntakeService.approve() re-compares this snapshot against the
    contact's live value before ever writing, and refuses (see
    StaleFieldConflict) rather than blindly overwriting newer CRM data.

    `field_label` is a snapshot of the human-readable label too (not
    re-derived at render time) -- same "snapshot, not live join" instinct
    used elsewhere in this app (CrmContact.source_snapshot,
    ActivityEvent.entity_name).
    """

    field_key: str  # "company", "custom:investment_industry", ...
    field_label: str  # "Company", "Investment Industry", ...
    operation: EmailFieldChangeOperation
    current_value: Any
    proposed_value: Any
    source_text: str | None = None  # the exact excerpt this was extracted from, for reviewer trust


class EmailIntakeItem(BaseModel):
    intake_id: str
    gmail_message_id: str  # idempotency key -- unique in the store
    gmail_thread_id: str | None = None
    received_at: datetime
    sender: str
    recipients: list[str] = Field(default_factory=list)
    subject: str
    body_text: str
    attachments: list[EmailAttachmentMeta] = Field(default_factory=list)

    status: EmailIntakeStatus
    matched_contact_id: str | None = None
    matched_contact_name: str | None = None  # snapshot, same reason as ActivityEvent.entity_name
    matched_on: str | None = None  # "email" | "apollo_contact_id" | "linkedin_url" | "name_company" | "manual" | None
    proposal: list[EmailCrmFieldChange] = Field(default_factory=list)  # may legitimately be empty -- see module docstring

    error_message: str | None = None
    created_at: datetime  # ingestion timestamp
    reviewed_at: datetime | None = None


class EmailIntakeItemPage(BaseModel):
    items: list[EmailIntakeItem]
    total: int
    page: int
    page_size: int


# ---- Webhook request/response (POST /sync/email-intake) -------------------


class EmailIntakeWebhookRequest(BaseModel):
    """
    Deliberately a plain, self-contained payload shape -- a Phase 2 Apps
    Script bridge would build exactly this from GmailApp's message object;
    Phase 1 test payloads build it by hand. No Gmail credential of any kind
    travels in this body.
    """

    gmail_message_id: str = Field(min_length=1)
    gmail_thread_id: str | None = None
    sender: str = Field(min_length=1)
    recipients: list[str] = Field(default_factory=list)
    subject: str = ""
    body_text: str = ""
    received_at: datetime
    attachments: list[EmailAttachmentMeta] = Field(default_factory=list)


class EmailIntakeWebhookResult(BaseModel):
    intake_id: str
    status: EmailIntakeStatus
    already_processed: bool = False  # True when gmail_message_id was already known -- no new work was done
    matched_contact_id: str | None = None
    matched_on: str | None = None
    proposal_field_count: int = 0


# ---- Review/approval API ---------------------------------------------------


class ManualMatchRequest(BaseModel):
    crm_contact_id: str = Field(min_length=1)


class ApproveEmailIntakeRequest(BaseModel):
    """`field_keys` is the reviewer's checked selection at the moment they
    click Approve -- there is no separate server-side "toggle checkbox"
    endpoint or persisted per-field approval flag; the selection is
    submitted explicitly, once, here."""

    field_keys: list[str] = Field(min_length=1)


class StaleFieldConflict(BaseModel):
    """One approved field whose live CRM value no longer matches what this
    proposal's current_value snapshot recorded when it was generated."""

    field_key: str
    field_label: str
    reviewed_value: Any  # current_value as originally snapshotted on the proposal
    live_value: Any  # the contact's actual value right now
    proposed_value: Any


class ApproveEmailIntakeResult(BaseModel):
    """
    `status="approved"`: `item` reflects the now-Approved intake item.
    `status="stale"`: nothing was written; `conflicts` lists every
    requested field whose live value drifted from the snapshot, and `item`
    is the SAME intake item with every proposal row's `current_value`
    refreshed to the live contact (see EmailIntakeService.approve()'s
    docstring for why this is the smallest safe "Refresh Proposal" -- no
    separate refresh endpoint is needed).
    """

    status: str  # "approved" | "stale"
    item: EmailIntakeItem
    conflicts: list[StaleFieldConflict] = Field(default_factory=list)
