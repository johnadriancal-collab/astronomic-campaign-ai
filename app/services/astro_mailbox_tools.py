"""
Astro AI Phase 3 -- read-only connected-mailbox tools.

STRUCTURAL credential protection, not just convention: this class is
constructed with a `MailboxStore` directly (never a `MailboxService`,
which internally also holds a `MailboxCredentialStore` reference) -- see
__init__ below. `MailboxStore` only knows the `Mailbox` type; it has no
method that could return `MailboxCredential` data even in principle, so
there is no code path from this file to an encrypted refresh token, an
access token, or the mailbox encryption key, regardless of what a future
edit to this file might attempt. `MailboxCredentialStore` is never
imported here.

"Deliverability Index" / "Emails Sent Today" / "Queue" / "Campaigns" shown
in the CRM UI have NO backing field on `Mailbox` at all (confirmed via the
Phase 3 architecture investigation) -- this module never fabricates them;
the tool descriptions and projections below only ever surface real
`Mailbox` fields.
"""

from loguru import logger

from app.models.mailbox import Mailbox
from app.repositories.mailbox_store import MailboxStore

MAILBOX_LIST_LIMIT = 50
_LOOKUP_CANDIDATE_LIMIT = 5

ASTRO_MAILBOX_TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "list_connected_mailboxes",
        "description": (
            "List every connected mailbox (email, display name, provider, connection status, "
            "connected date, granted OAuth scopes). Returns the true total, capped at 50 "
            "records. There is no sending, deliverability, queue, or campaign-count data "
            "available for mailboxes -- do not ask for or report any of that."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_mailbox",
        "description": (
            "Look up one connected mailbox by its exact email address (preferred) or display "
            "name. If more than one mailbox could match a name, this returns an 'ambiguous' "
            "result listing the possible matches instead of picking one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "email": {"type": "string", "description": "Exact email address, e.g. victoria@astronomicconnect.com."},
                "name": {"type": "string", "description": "Display name to match, if email isn't known."},
            },
            "required": [],
        },
    },
]


def _project_mailbox(mailbox: Mailbox) -> dict:
    return {
        "email": mailbox.email,
        "display_name": mailbox.display_name,
        "provider": mailbox.provider.value,
        "status": mailbox.status.value,
        "connected_at": mailbox.connected_at.isoformat(),
        "disconnected_at": mailbox.disconnected_at.isoformat() if mailbox.disconnected_at else None,
        "granted_scopes": mailbox.granted_scopes,
    }


class AstroMailboxTools:
    """Read-only mailbox tool surface. Holds only a `MailboxStore` -- see
    module docstring for why that alone is the credential-safety
    guarantee, not just a convention."""

    def __init__(self, mailbox_store: MailboxStore):
        self.mailbox_store = mailbox_store

    async def dispatch(self, name: str, tool_input: dict) -> dict:
        handler = _HANDLERS.get(name)
        if handler is None:
            return {"error": "unknown_tool", "message": f"'{name}' is not an available tool."}
        try:
            return await handler(self, tool_input or {})
        except (KeyError, TypeError, ValueError) as e:
            return {"error": "invalid_filter", "message": f"Malformed tool input: {e}"}
        except Exception as e:  # noqa: BLE001 -- must never crash the chat turn
            logger.error(f"Astro mailbox tool '{name}' failed: {type(e).__name__}")
            return {"error": "tool_failed", "message": "The mailbox lookup failed -- please try again."}

    async def _list_connected_mailboxes(self, tool_input: dict) -> dict:
        mailboxes = await self.mailbox_store.list()
        total = len(mailboxes)
        returned = mailboxes[:MAILBOX_LIST_LIMIT]
        return {
            "total": total,
            "returned": len(returned),
            "mailboxes": [_project_mailbox(m) for m in returned],
        }

    async def _get_mailbox(self, tool_input: dict) -> dict:
        email = (tool_input.get("email") or "").strip()
        name = (tool_input.get("name") or "").strip()

        if email:
            mailbox = await self.mailbox_store.get_by_email(email)
            if mailbox is None:
                return {"status": "not_found"}
            return {"status": "found", "mailbox": _project_mailbox(mailbox)}

        if not name:
            return {"error": "invalid_filter", "message": "Provide an email or a display name to look up a mailbox."}

        all_mailboxes = await self.mailbox_store.list()
        matches = [m for m in all_mailboxes if (m.display_name or "").strip().lower() == name.lower()]
        if not matches:
            return {"status": "not_found"}
        if len(matches) == 1:
            return {"status": "found", "mailbox": _project_mailbox(matches[0])}
        return {
            "status": "ambiguous",
            "total": len(matches),
            "candidates": [_project_mailbox(m) for m in matches[:_LOOKUP_CANDIDATE_LIMIT]],
        }


_HANDLERS = {
    "list_connected_mailboxes": AstroMailboxTools._list_connected_mailboxes,
    "get_mailbox": AstroMailboxTools._get_mailbox,
}
