"""
Agent responsible for turning a plain-English campaign request into a
structured CampaignPlan via Claude.
"""

from pathlib import Path

from app.claude.client import ClaudeClient
from app.models.campaign import CampaignPlan

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "campaign_prompt.md"


class CampaignAgent:
    def __init__(self, claude_client: ClaudeClient | None = None):
        self.claude = claude_client or ClaudeClient()
        self.system_prompt = PROMPT_PATH.read_text()

    async def generate_campaign_plan(self, user_prompt: str) -> CampaignPlan:
        raw = await self.claude.generate_json(self.system_prompt, user_prompt)
        return CampaignPlan(**raw)
