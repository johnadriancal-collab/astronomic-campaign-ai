"""
Claude as the ranking engine: decides which prospects from a candidate pool
are the best fit for a specific campaign, using holistic judgment rather
than deterministic keyword scoring (see prospect_selector.py for why that
approach hit its ceiling).

Division of responsibility, deliberately narrow:
    Apollo retrieves. ProspectSelector partitions by email (a hard business
    rule, not a quality judgment). ProspectRanker judges quality and returns
    an order. Claude here does NOT regenerate filters and does NOT search
    Apollo -- it only ranks the exact pool it's handed.

Cost/call discipline: exactly ONE Claude call per ranking (no per-prospect
calls, no follow-up calls). Every field on every prospect Apollo returned is
passed through to Claude unmodified -- no field is removed, compressed, or
summarized, so the model always has full context (and automatically gains
access to any new field Apollo starts returning in the future, with no code
change needed here).
"""

import json
import time
from pathlib import Path

from loguru import logger

from app.claude.client import ClaudeClient
from app.models.campaign import CampaignPlan

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "prospect_ranking_prompt.md"

# Sized for up to ~100 ranked entries with a short reason each; see
# ranking_cost_estimate() in the same module for why this is generous
# rather than tight.
MAX_OUTPUT_TOKENS = 4096
# Holistic judgment should be more consistent than campaign copywriting --
# lower than CampaignAgent's temperature (0.4), which is creative writing.
RANKING_TEMPERATURE = 0.2

# Prints a debug summary (pool size, score stats, latency, token usage) for
# every rank() call when True. Independent of any per-call return value --
# ranker.rank() always returns the full detail regardless of this flag.
DEBUG = False


class ProspectRanker:
    def __init__(self, claude_client: ClaudeClient | None = None):
        self.claude = claude_client or ClaudeClient()
        self.system_prompt = PROMPT_PATH.read_text()

    def build_prompt(
        self,
        user_prompt: str,
        plan: CampaignPlan,
        prospects: list[dict],
        target_count: int,
    ) -> str:
        """
        Builds the user message: campaign context plus every prospect
        exactly as Apollo returned it (no field removed/compressed/summarized).
        """
        payload = {
            "campaign_objective": user_prompt,
            "campaign_name": plan.campaign_name,
            "campaign_filters": plan.filters.model_dump(),
            "campaign_emails": [step.model_dump() for step in plan.sequence],
            "target_prospect_count": target_count,
            "prospects": prospects,
        }
        return json.dumps(payload, indent=2)

    def parse_response(self, raw: dict, prospects: list[dict]) -> list[dict]:
        """
        Matches each returned apollo_person_id back to its full raw person
        object and attaches claude_score/claude_reason to a COPY of it
        (never mutates the input list). Entries referencing an id that
        wasn't in the prospect pool are dropped, never silently invented.
        """
        by_id = {p.get("id"): p for p in prospects}
        ranked: list[dict] = []

        for entry in raw.get("ranked_prospects", []):
            person_id = entry.get("apollo_person_id")
            person = by_id.get(person_id)
            if person is None:
                logger.warning(f"Claude ranked unknown apollo_person_id {person_id!r}; dropping")
                continue
            ranked.append(
                {**person, "claude_score": entry.get("score"), "claude_reason": entry.get("reason")}
            )

        return ranked

    async def rank(
        self,
        user_prompt: str,
        plan: CampaignPlan,
        prospects: list[dict],
        target_count: int = 25,
    ) -> dict:
        """
        Sends the full prospect pool to Claude in ONE call and returns the
        top target_count, ranked. Claude only ranks -- it never touches
        Apollo or the plan's filters.
        """
        user_message = self.build_prompt(user_prompt, plan, prospects, target_count)

        start = time.monotonic()
        raw, usage = await self.claude.generate_json_with_usage(
            self.system_prompt,
            user_message,
            max_tokens=MAX_OUTPUT_TOKENS,
            temperature=RANKING_TEMPERATURE,
        )
        latency_seconds = time.monotonic() - start

        ranked = self.parse_response(raw, prospects)

        result = {
            "ranked": ranked,
            "usage": usage,
            "latency_seconds": latency_seconds,
            "pool_size": len(prospects),
        }

        if DEBUG:
            self._print_debug_summary(result)

        return result

    @staticmethod
    def _print_debug_summary(result: dict) -> None:
        ranked = result["ranked"]
        usage = result["usage"]
        scores = [p["claude_score"] for p in ranked if p.get("claude_score") is not None]
        avg_score = sum(scores) / len(scores) if scores else 0

        print(f"\nApollo returned: {result['pool_size']}")
        print(f"Claude selected: {len(ranked)}")
        print(f"Average score: {avg_score:.1f}")
        print(f"Lowest score accepted: {min(scores) if scores else 'N/A'}")
        # Claude's contract only returns scores for prospects it selected --
        # computing this would require asking it to score the full pool,
        # which increases output tokens for a debug-only metric.
        print("Highest score rejected: N/A (Claude only scores prospects it selects)")
        print(f"Ranking latency: {result['latency_seconds']:.2f}s")
        print(
            f"Claude token usage: input={usage.get('input_tokens')}, "
            f"output={usage.get('output_tokens')}"
        )
