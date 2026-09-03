"""
MailCampaignCsvProspectService -- Stage 4B (2026-09-03) orchestration for
Campaign Manager's "Upload CSV" Add Prospects flow. The ONE component in
this codebase that legitimately holds a WRITABLE CrmImportService
reference -- see MailCampaignService's own module-level docstring
(CrmImportResolutionReader) for why that file itself must never do this.
This service's whole job is composing CrmImportService.commit() with
MailCampaignService.add_prospects(), durably linked by a
MailCampaignCsvProspectLink so one logical operation can never fan out
into more than one CRM import commit, regardless of how many times the
client retries or what import_batch_id a retry happens to (re)supply.

APPROVED ORDERING (2026-09-03 design review):
    resolve/create durable link
    -> read-only campaign eligibility preflight
    -> CrmImportService.commit() (unconditional -- Stage 4A's own
       idempotency already handles fresh/resumed/already-COMMITTED
       transparently; this service NEVER inspects or branches on
       CrmImportBatch.status itself, deliberately not duplicating Stage
       4A's own state machine)
    -> MailCampaignService.add_prospects() (its own authoritative
       eligibility check runs again here, unconditionally -- the
       preflight above is a courtesy that avoids predictable CRM writes
       for an already-doomed request; it is never a replacement)

CRITICAL LINK SEMANTICS: if a link already exists for
(mail_campaign_id, idempotency_key), its OWN recorded import_batch_id
ALWAYS wins over whatever the current request happens to carry -- a
retry that (accidentally or otherwise) supplies a different
import_batch_id is silently ignored in favor of the original. One
(campaign, key) pair can only ever be bound to one CrmImportBatch, from
the very first time this method is ever called for it, regardless of
what happens downstream on that first call (even an eligibility
rejection still leaves the link pointing at whatever import_batch_id was
supplied then -- see add_prospects_from_csv()'s own docstring).

THE UNAVOIDABLE RACE (documented and accepted, not "fixed" -- no
distributed transaction is used or needed, per the approved design):
preflight sees an eligible campaign -> commit() succeeds, creating/
updating real CrmContacts -> the campaign is archived by someone else
before add_prospects() runs its own authoritative check ->
add_prospects() raises MailCampaignNotEligibleForProspectsError. Because
no campaign can ever transition back to DRAFT/READY once ACTIVE/PAUSED/
COMPLETED, the ONLY real transition into ineligibility during this
window is archive_campaign() -- so this race has exactly one shape, and
its outcome (a permanent rejection for this specific (campaign, key)
pair, since ARCHIVED is already terminal everywhere else in this
codebase) is not a new failure mode, just that same existing terminal
behavior surfacing one call later than ideal. The CRM commit itself is
never rolled back -- the contacts are real, valid CRM data, independent
of any one campaign's fate, exactly as valuable as any standalone CRM
import. A retry of the same (campaign, key) pair after this race always
reuses the same link and the same already-committed import_batch_id --
this service never re-derives or re-resolves either.
"""

from datetime import datetime, timezone

from app.models.mail import (
    MailCampaignCsvProspectLink,
    MailEnrollmentBatch,
    MailEnrollmentBatchSource,
)
from app.repositories.mail_campaign_csv_prospect_link_store import (
    DuplicateCsvProspectLinkError,
    MailCampaignCsvProspectLinkStore,
)
from app.services.crm_import_service import CrmImportService
from app.services.mail_campaign_service import (
    PROSPECT_ELIGIBLE_CAMPAIGN_STATUSES,
    MailCampaignNotEligibleForProspectsError,
    MailCampaignService,
)


class MailCampaignCsvProspectService:
    def __init__(
        self,
        crm_import_service: CrmImportService,
        mail_campaign_service: MailCampaignService,
        link_store: MailCampaignCsvProspectLinkStore,
    ):
        self.crm_import_service = crm_import_service
        self.mail_campaign_service = mail_campaign_service
        self.link_store = link_store

    async def add_prospects_from_csv(
        self,
        mail_campaign_id: str,
        idempotency_key: str,
        import_batch_id: str,
        decisions: dict[int, str] | None = None,
        actor: str | None = None,
    ) -> MailEnrollmentBatch:
        """See this module's own docstring for the full ordering, the
        link-wins-over-request-body guarantee, and the documented
        eligibility race. Propagates, unmodified: MailCampaignNotFound
        (bad mail_campaign_id), MailCampaignNotEligibleForProspectsError
        (preflight OR add_prospects()'s own authoritative check),
        CrmImportBatchNotFound / ValueError (bad or not-yet-committed
        import_batch_id, from CrmImportService.commit() or
        list_resolved_contact_ids()) -- callers (the API route) map these
        to HTTP responses exactly as the existing CRM_LIST branch already
        does for its own equivalents."""
        link = await self.link_store.get_by_idempotency_key(mail_campaign_id, idempotency_key)
        if link is None:
            candidate_link = MailCampaignCsvProspectLink(
                mail_campaign_id=mail_campaign_id,
                idempotency_key=idempotency_key,
                import_batch_id=import_batch_id,
                created_at=datetime.now(timezone.utc),
            )
            try:
                await self.link_store.create(candidate_link)
                link = candidate_link
            except DuplicateCsvProspectLinkError:
                # Concurrent race: another request for this exact
                # (campaign, key) pair won first. Use ITS link, never our
                # own candidate -- same "loser reconciles to the winner"
                # pattern as MailEnrollmentBatchStore's own idempotency
                # race (see MailCampaignService.add_prospects()).
                link = await self.link_store.get_by_idempotency_key(mail_campaign_id, idempotency_key)
                assert link is not None  # the collision itself proves one exists

        linked_import_batch_id = link.import_batch_id  # ALWAYS wins over the request's own import_batch_id -- see module docstring

        campaign = await self.mail_campaign_service.get_campaign(mail_campaign_id)
        if campaign.status not in PROSPECT_ELIGIBLE_CAMPAIGN_STATUSES:
            raise MailCampaignNotEligibleForProspectsError(mail_campaign_id, campaign.status)

        # Unconditional -- Stage 4A's own idempotency already makes this
        # a safe no-op (already COMMITTED), a clean resume (COMMITTING),
        # or a fresh commit (MAPPED), whichever applies. This service
        # never branches on CrmImportBatch.status itself.
        await self.crm_import_service.commit(linked_import_batch_id, decisions)

        return await self.mail_campaign_service.add_prospects(
            mail_campaign_id,
            source=MailEnrollmentBatchSource.CSV_UPLOAD,
            idempotency_key=idempotency_key,
            source_import_batch_id=linked_import_batch_id,
            actor=actor,
        )
