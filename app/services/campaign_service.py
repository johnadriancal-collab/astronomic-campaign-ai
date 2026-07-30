"""
Orchestrates the full campaign pipeline: Claude generates the plan, then
Apollo builds the list/sequence/steps and enrolls prospects.

Design note on launching:
`CampaignPlan.launch` is a field Claude returns based on its read of the
prompt, but it is NOT wired to actually activate a sequence. Whether real
emails go out to real people is controlled only by the explicit
`auto_launch` argument to `build_campaign` (set by a human via the API
caller), or by a separate call to `launch()`. A model's own generated
output shouldn't be the sole trigger for sending live outreach.
"""

from loguru import logger

from app.agents.campaign_agent import CampaignAgent
from app.apollo import ApolloClient
from app.config import settings
from app.models.campaign import CampaignExecutionReport, CampaignPlan


class CampaignService:
    def __init__(self, agent: CampaignAgent | None = None, apollo: ApolloClient | None = None):
        self.agent = agent or CampaignAgent()
        self.apollo = apollo or ApolloClient()

    async def preview(self, user_prompt: str) -> CampaignPlan:
        """Returns Claude's generated plan only — no Apollo calls."""
        return await self.agent.generate_campaign_plan(user_prompt)

    async def build_campaign(
        self, user_prompt: str, auto_launch: bool = False
    ) -> tuple[CampaignPlan, CampaignExecutionReport]:
        plan = await self.agent.generate_campaign_plan(user_prompt)
        report = CampaignExecutionReport(campaign_name=plan.campaign_name)

        try:
            search_results = await self.apollo.search_people(plan.filters.model_dump())
            people = search_results.get("people", [])
            report.prospects_found = len(people)
        except Exception as e:
            logger.error(f"Prospect search failed: {e}")
            report.errors.append(f"Prospect search failed: {e}")
            return plan, report

        try:
            list_resp = await self.apollo.create_list(plan.campaign_name)
            list_id = list_resp.get("label", {}).get("id") or list_resp.get("id")
            report.apollo_list_id = list_id
        except Exception as e:
            logger.error(f"List creation failed: {e}")
            report.errors.append(f"List creation failed: {e}")
            return plan, report

        # List membership is set via `label_names` at contact-creation time --
        # Apollo has no separate "add existing contacts to a list" endpoint.
        contact_ids: list[str] = []
        for person in people:
            try:
                contact_resp = await self.apollo.create_contact(
                    {
                        "first_name": person.get("first_name"),
                        "last_name": person.get("last_name_obfuscated"),
                        "email": person.get("email"),
                        "organization_name": (person.get("organization") or {}).get("name"),
                        "title": person.get("title"),
                        "label_names": [plan.campaign_name],
                    }
                )
                cid = contact_resp.get("contact", {}).get("id") or contact_resp.get("id")
                if cid:
                    contact_ids.append(cid)
            except Exception as e:
                logger.warning(f"Contact creation failed for {person.get('id')}: {e}")
                report.errors.append(f"Contact creation failed for {person.get('id')}: {e}")

        try:
            seq_resp = await self.apollo.create_sequence(plan.campaign_name)
            sequence_id = seq_resp.get("emailer_campaign", {}).get("id") or seq_resp.get("id")
            report.apollo_sequence_id = sequence_id
        except Exception as e:
            logger.error(f"Sequence creation failed: {e}")
            report.errors.append(f"Sequence creation failed: {e}")
            return plan, report

        if plan.sequence:
            try:
                await self.apollo.add_sequence_steps(
                    sequence_id, [step.model_dump() for step in plan.sequence]
                )
            except Exception as e:
                logger.warning(f"Adding sequence steps failed: {e}")
                report.errors.append(f"Adding sequence steps failed: {e}")

        if contact_ids:
            mailbox_id = settings.default_sender_mailbox_id
            if not mailbox_id:
                try:
                    accounts_resp = await self.apollo.list_email_accounts()
                    accounts = accounts_resp.get("email_accounts", [])
                    if accounts:
                        mailbox_id = accounts[0].get("id")
                except Exception as e:
                    logger.warning(f"Could not list Apollo email accounts: {e}")

            if mailbox_id:
                try:
                    await self.apollo.enroll_contacts(sequence_id, contact_ids, mailbox_id)
                    report.prospects_enrolled = len(contact_ids)
                except Exception as e:
                    logger.error(f"Enrollment failed: {e}")
                    report.errors.append(f"Enrollment failed: {e}")
            else:
                report.errors.append(
                    "Enrollment skipped: no sender mailbox available -- set "
                    "DEFAULT_SENDER_MAILBOX_ID or connect an email account in Apollo"
                )

        if auto_launch:
            try:
                await self.apollo.activate_sequence(sequence_id)
                report.activated = True
            except Exception as e:
                logger.error(f"Activation failed: {e}")
                report.errors.append(f"Activation failed: {e}")

        return plan, report

    async def launch(self, sequence_id: str) -> CampaignExecutionReport:
        """Explicit human-confirmed activation of an already-built sequence."""
        await self.apollo.activate_sequence(sequence_id)
        return CampaignExecutionReport(
            campaign_name="", apollo_sequence_id=sequence_id, activated=True
        )
