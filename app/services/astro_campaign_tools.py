"""
Astro AI Phase 3 -- read-only Campaign Manager tools.

Apollo campaigns (Campaign, app/models/campaign.py) and Astronomic Mail
campaigns (MailCampaign, app/models/mail.py) remain TWO SEPARATE SYSTEMS
here, exactly as they are everywhere else in this app -- this module never
merges them into one model, only into a small presentation-shaped summary
per campaign, and every projection is explicit about which system a
campaign belongs to and what data actually exists for it. Status-bucket
mapping is reused verbatim from app/models/campaign_manager.py (the same
dicts app/api/campaign_manager.py's dashboard uses) rather than
re-derived.

Apollo sequence statistics (EmailSequence.unique_*) are real, Apollo-
sourced numbers, but ONLY populated by an explicit manual sync
(POST /campaign/{id}/sequence/sync) -- never automatically. get_campaign
below NEVER interprets a missing/never-synced EmailSequence as "zero
activity": it explicitly reports whether a sequence exists, whether it's
ever been synced, and the sync timestamp when it has.

Astronomic Mail's MailCampaignReview gives contacts_eligible/
theoretical_total_sends -- a THEORETICAL, planned-audience calculation
(current CRM list membership x current suppression state x current step
count), never a real send count, because Astronomic Mail has no sending
capability at all yet. Every field this module returns for a Mail
campaign is labeled accordingly (see the "audience_theoretical" key's own
"note" field in _get_campaign below).

No get_campaign_stats tool exists (per the approved architecture) --
campaign statistics are folded into get_campaign's response, specifically
to avoid a bare "stats" tool inviting the theoretical-vs-real conflation
described above.

Strictly read-only: this module only ever calls CampaignService.store.list
(never .preview/.search/.build/.mark_ready/.activate/.pause),
MailCampaignService.list_campaigns/.get_review (never .create_campaign/
.update_campaign/.mark_ready/.unlock_campaign/.archive_campaign/
.add_step/.update_step/.delete_step),
MailSuppressionService.list_active_suppressed_emails (never .suppress/
.unsuppress), and EmailSequenceStore.get_by_campaign_id (never .save/
.create, and never any Apollo API client at all -- no sync is ever
triggered from this file).
"""

from loguru import logger

from app.models.campaign import Campaign
from app.models.campaign_manager import APOLLO_STATUS_BUCKET, MAIL_STATUS_BUCKET, CampaignStatusBucket, SendingMethod
from app.models.mail import MailCampaign
from app.repositories.email_sequence_store import EmailSequenceStore
from app.services.campaign_service import CampaignService
from app.services.mail_campaign_service import MailCampaignService
from app.services.mail_suppression_service import MailSuppressionService

CAMPAIGN_LIST_LIMIT = 50
_LOOKUP_CANDIDATE_LIMIT = 5

ASTRO_CAMPAIGN_TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "list_campaigns",
        "description": (
            "List campaigns across BOTH sending systems -- Apollo and Astronomic Mail. These are "
            "different systems with different data: Astronomic Mail has no send/open/click "
            "statistics at all (it cannot send email yet); Apollo has real statistics ONLY for "
            "campaigns whose sequence has been manually synced. Returns the true total, capped "
            "at 50. Optionally filter by status_bucket or sending_method."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "status_bucket": {
                    "type": "string",
                    "enum": [b.value for b in CampaignStatusBucket],
                    "description": "draft, in_progress, ready, active, paused, failed, or archived.",
                },
                "sending_method": {"type": "string", "enum": [m.value for m in SendingMethod]},
                "limit": {"type": "integer", "description": "Max campaigns to return. Capped at 50."},
            },
            "required": [],
        },
    },
    {
        "name": "get_campaign",
        "description": (
            "Look up one campaign by its exact name. Names are not guaranteed unique across "
            "either system -- if more than one campaign matches, this returns an 'ambiguous' "
            "result instead of picking one. For an Apollo campaign, 'sequence_stats' is null if "
            "no sequence has been deployed yet, has synced=false (never interpret this as zero "
            "activity) if deployed but never synced, or has real unique_scheduled/delivered/"
            "opened/clicked/replied/bounced counts plus last_synced_at if it has been synced. "
            "For an Astronomic Mail campaign, 'audience_theoretical' is a THEORETICAL planned-"
            "audience estimate (current list membership x suppression x step count), never "
            "actual sends -- always say so, never present it as real sending activity. Apollo "
            "campaigns do not have a CRM list relationship (only Astronomic Mail does, via "
            "source_list_id) -- their response never includes one; do not infer one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "count_campaigns",
        "description": (
            "Count campaigns across both sending systems, optionally filtered by status_bucket "
            "or sending_method. Returns ONLY a total, never campaign records."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "status_bucket": {"type": "string", "enum": [b.value for b in CampaignStatusBucket]},
                "sending_method": {"type": "string", "enum": [m.value for m in SendingMethod]},
            },
            "required": [],
        },
    },
]


def _apollo_bucket(campaign: Campaign) -> CampaignStatusBucket:
    return APOLLO_STATUS_BUCKET[campaign.status.value]


def _mail_bucket(campaign: MailCampaign) -> CampaignStatusBucket:
    return MAIL_STATUS_BUCKET[campaign.status.value]


def _project_apollo_summary(campaign: Campaign) -> dict:
    return {
        "id": campaign.campaign_id,
        "sending_method": SendingMethod.APOLLO.value,
        "name": campaign.plan.campaign_name,
        "status": campaign.status.value,
        "status_bucket": _apollo_bucket(campaign).value,
        "created_at": campaign.created_at.isoformat(),
    }


def _project_mail_summary(campaign: MailCampaign) -> dict:
    return {
        "id": campaign.mail_campaign_id,
        "sending_method": SendingMethod.ASTRONOMIC_MAIL.value,
        "name": campaign.name,
        "status": campaign.status.value,
        "status_bucket": _mail_bucket(campaign).value,
        "created_at": campaign.created_at.isoformat(),
    }


class AstroCampaignTools:
    """Read-only Campaign Manager tool surface -- see module docstring for
    the exact, closed set of read methods this ever calls."""

    def __init__(
        self,
        campaign_service: CampaignService,
        mail_campaign_service: MailCampaignService,
        mail_suppression_service: MailSuppressionService,
        email_sequence_store: EmailSequenceStore,
    ):
        self.campaign_service = campaign_service
        self.mail_campaign_service = mail_campaign_service
        self.mail_suppression_service = mail_suppression_service
        self.email_sequence_store = email_sequence_store

    async def dispatch(self, name: str, tool_input: dict) -> dict:
        handler = _HANDLERS.get(name)
        if handler is None:
            return {"error": "unknown_tool", "message": f"'{name}' is not an available tool."}
        try:
            return await handler(self, tool_input or {})
        except (KeyError, TypeError, ValueError) as e:
            return {"error": "invalid_filter", "message": f"Malformed tool input: {e}"}
        except Exception as e:  # noqa: BLE001 -- must never crash the chat turn
            logger.error(f"Astro campaign tool '{name}' failed: {type(e).__name__}")
            return {"error": "tool_failed", "message": "The campaign lookup failed -- please try again."}

    async def _all_summaries(self) -> list[dict]:
        apollo_campaigns = await self.campaign_service.store.list()
        mail_campaigns = await self.mail_campaign_service.list_campaigns()
        summaries = [_project_apollo_summary(c) for c in apollo_campaigns]
        summaries += [_project_mail_summary(c) for c in mail_campaigns]
        summaries.sort(key=lambda s: s["created_at"], reverse=True)
        return summaries

    async def _list_campaigns(self, tool_input: dict) -> dict:
        summaries = await self._all_summaries()
        status_bucket = tool_input.get("status_bucket")
        sending_method = tool_input.get("sending_method")
        if status_bucket:
            summaries = [s for s in summaries if s["status_bucket"] == status_bucket]
        if sending_method:
            summaries = [s for s in summaries if s["sending_method"] == sending_method]

        total = len(summaries)
        requested_limit = int(tool_input.get("limit") or CAMPAIGN_LIST_LIMIT)
        limit = max(1, min(requested_limit, CAMPAIGN_LIST_LIMIT))
        returned = summaries[:limit]
        return {"total": total, "returned": len(returned), "campaigns": returned}

    async def _count_campaigns(self, tool_input: dict) -> dict:
        # `total` reflects the full filtered set regardless of the limit
        # passed here -- only `campaigns`/`returned` would be affected by
        # it, and this tool never returns those.
        result = await self._list_campaigns({**tool_input, "limit": 1})
        return {"total": result["total"]}

    async def _resolve_campaign_by_name(self, name: str) -> dict:
        apollo_campaigns = await self.campaign_service.store.list()
        mail_campaigns = await self.mail_campaign_service.list_campaigns()
        needle = name.strip().lower()
        apollo_matches = [c for c in apollo_campaigns if c.plan.campaign_name.strip().lower() == needle]
        mail_matches = [c for c in mail_campaigns if c.name.strip().lower() == needle]

        total = len(apollo_matches) + len(mail_matches)
        if total == 0:
            return {"status": "not_found"}
        if total == 1:
            if apollo_matches:
                return {"status": "found", "sending_method": "apollo", "campaign": apollo_matches[0]}
            return {"status": "found", "sending_method": "astronomic_mail", "campaign": mail_matches[0]}

        candidates = [_project_apollo_summary(c) for c in apollo_matches]
        candidates += [_project_mail_summary(c) for c in mail_matches]
        return {"status": "ambiguous", "total": total, "candidates": candidates[:_LOOKUP_CANDIDATE_LIMIT]}

    async def _get_campaign(self, tool_input: dict) -> dict:
        name = (tool_input.get("name") or "").strip()
        if not name:
            return {"error": "invalid_filter", "message": "Provide a campaign name to look up."}

        resolved = await self._resolve_campaign_by_name(name)
        if resolved["status"] != "found":
            return resolved

        if resolved["sending_method"] == "apollo":
            return await self._project_apollo_detail(resolved["campaign"])
        return await self._project_mail_detail(resolved["campaign"])

    async def _project_apollo_detail(self, campaign: Campaign) -> dict:
        sequence = await self.email_sequence_store.get_by_campaign_id(campaign.campaign_id)
        if sequence is None:
            stats = None  # no sequence deployed at all yet -- distinct from "deployed but never synced"
        elif sequence.last_synced_at is None:
            stats = {
                "synced": False,
                "message": (
                    "This campaign's sequence has never been synced -- no send/open/click/"
                    "reply/bounce data is available yet."
                ),
            }
        else:
            stats = {
                "synced": True,
                "last_synced_at": sequence.last_synced_at.isoformat(),
                "unique_scheduled": sequence.unique_scheduled,
                "unique_delivered": sequence.unique_delivered,
                "unique_opened": sequence.unique_opened,
                "unique_clicked": sequence.unique_clicked,
                "unique_replied": sequence.unique_replied,
                "unique_bounced": sequence.unique_bounced,
                "unique_unsubscribed": sequence.unique_unsubscribed,
            }
        return {
            "status": "found",
            "sending_method": "apollo",
            "campaign": {
                "id": campaign.campaign_id,
                "name": campaign.plan.campaign_name,
                "status": campaign.status.value,
                "status_bucket": _apollo_bucket(campaign).value,
                "created_at": campaign.created_at.isoformat(),
                "desired_prospect_count": campaign.desired_prospect_count,
                "total_matches": campaign.total_matches,
                "selected_prospect_count": campaign.selected_prospect_count,
                "contacts_enrolled": campaign.contacts_enrolled,
                "sequence_stats": stats,
            },
        }

    async def _project_mail_detail(self, campaign: MailCampaign) -> dict:
        suppressed = await self.mail_suppression_service.list_active_suppressed_emails()
        review = await self.mail_campaign_service.get_review(campaign.mail_campaign_id, suppressed)
        return {
            "status": "found",
            "sending_method": "astronomic_mail",
            "campaign": {
                "id": campaign.mail_campaign_id,
                "name": campaign.name,
                "status": campaign.status.value,
                "status_bucket": _mail_bucket(campaign).value,
                "created_at": campaign.created_at.isoformat(),
                "source_list_id": campaign.source_list_id,
                "source_list_name": review.source_list_name,
                "audience_theoretical": {
                    "note": (
                        "THEORETICAL planned audience, not actual sends -- Astronomic Mail has "
                        "no sending capability yet."
                    ),
                    "total_contacts": review.total_contacts,
                    "contacts_eligible": review.contacts_eligible,
                    "sequence_step_count": review.sequence_step_count,
                    "theoretical_total_sends": review.theoretical_total_sends,
                },
            },
        }


_HANDLERS = {
    "list_campaigns": AstroCampaignTools._list_campaigns,
    "get_campaign": AstroCampaignTools._get_campaign,
    "count_campaigns": AstroCampaignTools._count_campaigns,
}
