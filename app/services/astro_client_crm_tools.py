"""
Astro AI Phase 3 (2026-09-15) -- Client CRM read access (canonical Contact
Event History) and ONE narrow, pending-action-gated write tool (mark
Contact attendance at an existing Engagement).

Deliberately its own file, mirroring astro_crm_tools.py/astro_campaign_tools.py/
astro_mailbox_tools.py/astro_activity_tools.py's existing "one domain, one
file, one allowlisted dispatch" convention -- this is the Client CRM
domain's Astro surface, never merged into astro_crm_tools.py even though
both need contact resolution (shared via astro_crm_tools.resolve_crm_contact,
imported, never duplicated).

get_crm_contact_event_history reads ONLY ClientCrmService.list_contact_event_history()
-- the same canonical, EngagementParticipant-derived projection the Client
CRM's own Contact-facing card already uses. It is explicitly, permanently
DIFFERENT from custom_fields.dinners_attended (a legacy free-text field
astro_crm_tools.py's get_crm_contact already exposes on its own) -- this
file never reads or writes that field, and the system prompt (see
astro_ai_service.py) tells Astro never to blend the two.

mark_crm_contact_engagement_attendance is the ONE write tool here. It
resolves a Contact (exact/unambiguous only, via resolve_crm_contact) and
an Engagement (exact-after-normalization only, via
ClientCrmService.find_engagements() -- title required, engagement_date/
location/client_name optionally narrow further; never fuzzy-matched),
inspects whether an ACTIVE EngagementParticipant already exists for that
exact (engagement_id, crm_contact_id) pair, and PROPOSES (never executes
directly -- this is Phase 3's one "higher-impact" write) either creating
one (ParticipantSource.ASTRO_AI, attendance_status=ATTENDED) or updating
the existing one. The actual mutation only happens via
confirm_astro_action (see astro_crm_tools.py), against the SAME shared
AstroPendingActionStore instance this class is constructed with -- this
file never executes anything itself.

Never reachable from here: creating a Client, creating an Engagement,
editing anything about a Client, or any Engagement field other than this
one Participant's attendance_status/role.
"""

from datetime import date

from loguru import logger

from app.models.client_crm import (
    Engagement,
    ParticipantAttendanceStatus,
    ParticipantRole,
    ParticipantSource,
)
from app.services.astro_crm_tools import _contact_name, _contact_resolution_response, resolve_crm_contact
from app.services.astro_pending_action_store import AstroPendingActionStore
from app.services.client_crm_service import ClientCrmService, ClientNotFound, EngagementParticipantDuplicate
from app.services.crm_service import CrmService

_ENGAGEMENT_CANDIDATE_LIMIT = 5

ASTRO_CLIENT_CRM_TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "get_crm_contact_event_history",
        "description": (
            "The CANONICAL, structured Event History for ONE specific, named person -- every "
            "Engagement (dinner, sponsorship, etc.) they're associated with, each with its own "
            "date, location, role (Guest/Host/Sponsor/Speaker-Panelist/Client/Astronomic Team/"
            "Other), RSVP status (invited/confirmed/declined), and attendance status (attended/"
            "no_show/cancelled). This is a DIFFERENT, more complete source than the "
            "dinners_attended field get_crm_contact returns -- dinners_attended is a legacy, "
            "free-text historical field; THIS is the current structured source of truth. Never "
            "merge or average the two, and never answer an Event-History-shaped question (which "
            "events, declined, role, date) from dinners_attended alone -- use this tool instead. "
            "An empty events list means no Engagement is on record for them, not that they've "
            "never attended anything Astronomic has ever run."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "first_name": {"type": "string"},
                "last_name": {"type": "string"},
                "company": {"type": "string"},
                "email": {"type": "string"},
            },
            "required": [],
        },
    },
    {
        "name": "mark_crm_contact_engagement_attendance",
        "description": (
            "Mark ONE specific, named person as ATTENDED at ONE specific, existing Engagement. "
            "This does NOT execute immediately -- it PROPOSES the change and returns a "
            "pending_action_id plus a plain-language description of exactly what would happen "
            "(including the CURRENT status, if any, e.g. 'change from Declined to Attended'); "
            "state that to the user and wait for their explicit confirmation in a LATER message "
            "before calling confirm_astro_action -- never in the same turn, never assumed. "
            "engagement_title is required and must be the Engagement's real title; "
            "engagement_date (an ISO date like 2026-09-10), location, and client_name are "
            "optional and only needed to disambiguate when more than one Engagement shares that "
            "title (e.g. a recurring series) -- an 'engagement_ambiguous' result lists the real "
            "candidates (id/title/date/location/client) for you to narrow with. NEVER invent or "
            "guess an Engagement, and NEVER create one -- if none matches, say so; do not offer "
            "to create it. role is optional (one of guest, client, host, speaker_panelist, "
            "astronomic_team, sponsor, other) -- if omitted, a NEW Participant defaults to guest "
            "and an EXISTING Participant's role is left exactly as it is; only pass role if the "
            "user actually specified one. If a Participant record already exists for this exact "
            "Contact+Engagement, this updates it (never creates a duplicate)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "first_name": {"type": "string"},
                "last_name": {"type": "string"},
                "company": {"type": "string"},
                "email": {"type": "string"},
                "engagement_title": {"type": "string"},
                "engagement_date": {"type": "string", "description": "ISO date, e.g. 2026-09-10. Optional, narrows an ambiguous title."},
                "location": {"type": "string", "description": "Optional, narrows an ambiguous title."},
                "client_name": {"type": "string", "description": "Optional, narrows an ambiguous title."},
                "role": {
                    "type": "string",
                    "enum": ["guest", "client", "host", "speaker_panelist", "astronomic_team", "sponsor", "other"],
                    "description": "Only pass this if the user explicitly specified a role.",
                },
            },
            "required": ["engagement_title"],
        },
    },
]


def _project_event_entry(entry) -> dict:
    return {
        "engagement_id": entry.engagement_id,
        "event_name": entry.event_name,
        "client_name": entry.client_name,
        "location": entry.location,
        "engagement_date": entry.engagement_date.isoformat() if entry.engagement_date else None,
        "engagement_type": entry.engagement_type.value,
        "dinner_type": entry.dinner_type.value if entry.dinner_type else None,
        "role": entry.role.value,
        "rsvp_status": entry.rsvp_status.value if entry.rsvp_status else None,
        "attendance_status": entry.attendance_status.value if entry.attendance_status else None,
    }


async def _project_engagement_candidate(engagement: Engagement, client_crm_service: ClientCrmService) -> dict:
    try:
        client = await client_crm_service.get_client(engagement.client_id)
        client_name = client.name
    except ClientNotFound:
        client_name = None
    return {
        "engagement_id": engagement.engagement_id,
        "title": engagement.title,
        "engagement_date": engagement.engagement_date.isoformat() if engagement.engagement_date else None,
        "location": engagement.location,
        "client_name": client_name,
    }


class AstroClientCrmTools:
    """Client CRM tool surface for Astro AI's Claude tool-use loop. See
    the module docstring for the full read/write architecture."""

    def __init__(
        self,
        client_crm_service: ClientCrmService,
        crm_service: CrmService,
        pending_action_store: AstroPendingActionStore | None = None,
    ):
        self.client_crm_service = client_crm_service
        # Needed ONLY for resolve_crm_contact (Contact resolution) -- this
        # class never queries/lists/exports Contacts beyond resolving the
        # ONE named person a given tool call is about.
        self.crm_service = crm_service
        self.pending_action_store = pending_action_store

    async def dispatch(self, name: str, tool_input: dict) -> dict:
        handler = _HANDLERS.get(name)
        if handler is None:
            return {"error": "unknown_tool", "message": f"'{name}' is not an available tool."}
        try:
            return await handler(self, tool_input or {})
        except (KeyError, TypeError, ValueError) as e:
            return {"error": "invalid_filter", "message": f"Malformed tool input: {e}"}
        except Exception as e:  # noqa: BLE001 -- must never crash the chat turn
            logger.error(f"Astro Client CRM tool '{name}' failed: {type(e).__name__}")
            return {"error": "tool_failed", "message": "The Client CRM lookup failed -- please try again."}

    async def _get_crm_contact_event_history(self, tool_input: dict) -> dict:
        resolution = await resolve_crm_contact(self.crm_service, tool_input)
        if resolution["status"] != "found":
            return _contact_resolution_response(resolution)
        contact = resolution["contact"]

        entries = await self.client_crm_service.list_contact_event_history(contact.crm_contact_id)
        return {
            "status": "found",
            "contact": {"name": _contact_name(contact)},
            "events": [_project_event_entry(e) for e in entries],
        }

    async def _mark_crm_contact_engagement_attendance(self, tool_input: dict) -> dict:
        if self.pending_action_store is None:
            return {"error": "tool_failed", "message": "Confirmation isn't available right now -- please try again."}

        engagement_title = (tool_input.get("engagement_title") or "").strip()
        if not engagement_title:
            return {"error": "invalid_filter", "message": "Provide the Engagement's title."}

        engagement_date_value: date | None = None
        raw_date = (tool_input.get("engagement_date") or "").strip()
        if raw_date:
            try:
                engagement_date_value = date.fromisoformat(raw_date)
            except ValueError:
                return {"error": "invalid_filter", "message": "engagement_date must be an ISO date, e.g. 2026-09-10."}

        role: ParticipantRole | None = None
        raw_role = (tool_input.get("role") or "").strip()
        if raw_role:
            try:
                role = ParticipantRole(raw_role)
            except ValueError:
                return {
                    "error": "invalid_filter",
                    "message": f"'{raw_role}' is not a valid role. Allowed: {[r.value for r in ParticipantRole]}.",
                }

        contact_resolution = await resolve_crm_contact(self.crm_service, tool_input)
        if contact_resolution["status"] != "found":
            return _contact_resolution_response(contact_resolution)
        contact = contact_resolution["contact"]
        contact_name = _contact_name(contact)

        engagements = await self.client_crm_service.find_engagements(
            title=engagement_title,
            engagement_date=engagement_date_value,
            location=(tool_input.get("location") or "").strip() or None,
            client_name=(tool_input.get("client_name") or "").strip() or None,
        )
        if not engagements:
            return {"status": "engagement_not_found"}
        if len(engagements) > 1:
            candidates = [await _project_engagement_candidate(e, self.client_crm_service) for e in engagements[:_ENGAGEMENT_CANDIDATE_LIMIT]]
            return {"status": "engagement_ambiguous", "total": len(engagements), "candidates": candidates}
        engagement = engagements[0]

        existing = await self.client_crm_service.get_active_participant_for_contact(
            engagement.engagement_id, contact.crm_contact_id
        )
        when = f" on {engagement.engagement_date.isoformat()}" if engagement.engagement_date else ""

        if existing is None:
            proposed_role = role or ParticipantRole.GUEST
            before = {"attendance_status": None, "rsvp_status": None, "role": None}
            description = (
                f'Mark {contact_name} as ATTENDED at "{engagement.title}"{when} '
                f"(new Participant record, role={proposed_role.value})."
            )

            async def _execute() -> dict:
                current = await self.client_crm_service.get_active_participant_for_contact(
                    engagement.engagement_id, contact.crm_contact_id
                )
                if current is not None:
                    return {
                        "error": "target_changed",
                        "message": "A Participant record for this Contact/Engagement was created since this "
                        "was proposed -- please check the current status and try again.",
                    }
                try:
                    participant = await self.client_crm_service.create_engagement_participant(
                        engagement.client_id,
                        engagement.engagement_id,
                        {
                            "crm_contact_id": contact.crm_contact_id,
                            "attendance_status": ParticipantAttendanceStatus.ATTENDED.value,
                            "role": proposed_role.value,
                        },
                        source=ParticipantSource.ASTRO_AI,
                    )
                except EngagementParticipantDuplicate:
                    return {
                        "error": "target_changed",
                        "message": "A Participant record for this Contact/Engagement already exists -- please try again.",
                    }
                return {
                    "status": "created",
                    "participant_id": participant.participant_id,
                    "attendance_status": participant.attendance_status.value,
                    "role": participant.role.value,
                }

        else:
            proposed_role = role or existing.role
            already_attended = existing.attendance_status == ParticipantAttendanceStatus.ATTENDED
            if already_attended and proposed_role == existing.role:
                return {
                    "status": "no_change",
                    "attendance_status": ParticipantAttendanceStatus.ATTENDED.value,
                    "role": existing.role.value,
                }

            from_label = (
                existing.attendance_status.value
                if existing.attendance_status
                else (existing.rsvp_status.value if existing.rsvp_status else "no status recorded")
            )
            role_clause = f", role changed to {proposed_role.value}" if role and role != existing.role else ""
            description = (
                f'Change {contact_name}\'s "{engagement.title}"{when} status from '
                f"{from_label} to Attended{role_clause}."
            )
            before = {
                "attendance_status": existing.attendance_status.value if existing.attendance_status else None,
                "rsvp_status": existing.rsvp_status.value if existing.rsvp_status else None,
                "role": existing.role.value,
            }
            existing_participant_id = existing.participant_id
            existing_snapshot = (existing.attendance_status, existing.rsvp_status, existing.role)

            async def _execute() -> dict:
                current = await self.client_crm_service.get_active_participant_for_contact(
                    engagement.engagement_id, contact.crm_contact_id
                )
                current_snapshot = (
                    (current.attendance_status, current.rsvp_status, current.role) if current is not None else None
                )
                if current is None or current.participant_id != existing_participant_id or current_snapshot != existing_snapshot:
                    return {
                        "error": "target_changed",
                        "message": "This Participant's status changed since this was proposed -- please check "
                        "the current status and try again.",
                    }
                # Real enum instances, NOT .value strings -- update_engagement_participant
                # applies `patch` via Pydantic's model_copy(update=...), which (unlike a
                # full model construction) does NOT validate/coerce a plain string into
                # the field's enum type. Passing .value here would silently leave
                # updated.attendance_status as the bare string "attended" rather than
                # ParticipantAttendanceStatus.ATTENDED, breaking the .value access below.
                patch = {"attendance_status": ParticipantAttendanceStatus.ATTENDED}
                if role:
                    patch["role"] = role
                try:
                    updated = await self.client_crm_service.update_engagement_participant(
                        engagement.client_id,
                        engagement.engagement_id,
                        existing_participant_id,
                        patch,
                        source=ParticipantSource.ASTRO_AI,
                    )
                except EngagementParticipantDuplicate:
                    return {"error": "target_changed", "message": "This Participant record changed unexpectedly -- please try again."}
                return {
                    "status": "updated",
                    "participant_id": updated.participant_id,
                    "attendance_status": updated.attendance_status.value,
                    "role": updated.role.value,
                }

        pending_action_id = self.pending_action_store.put(
            tool_name="mark_crm_contact_engagement_attendance",
            description=description,
            before=before,
            after={"attendance_status": ParticipantAttendanceStatus.ATTENDED.value, "role": proposed_role.value},
            execute=_execute,
        )
        return {"status": "pending_confirmation", "pending_action_id": pending_action_id, "description": description}


_HANDLERS = {
    "get_crm_contact_event_history": AstroClientCrmTools._get_crm_contact_event_history,
    "mark_crm_contact_engagement_attendance": AstroClientCrmTools._mark_crm_contact_engagement_attendance,
}
