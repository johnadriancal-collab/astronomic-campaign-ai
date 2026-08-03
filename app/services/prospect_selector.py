"""
Decides which prospects from a single Apollo search are worth contacting.

Architecture philosophy: Apollo finds candidates, Claude judges them. Apollo
is already good at search -- this module deliberately does not try to force
Apollo's coarse categorical filters into returning an exact count (see
campaign_service.py). Instead, Apollo is searched once for the requested
count, and ProspectSelector picks the best `target_count` out of whatever
comes back.

Hard priority rule: prospects with a usable email are ALWAYS ranked ahead of
prospects without one -- this is a hard filter, not one scoring factor among
several. Email-less prospects only fill remaining slots if there aren't
enough with-email prospects to reach target_count. `has_email` is Apollo's
own search-time signal (a boolean flag -- Apollo's free search endpoint
never reveals the actual address, only whether one exists on file), which is
exactly why it's handled as a partition upstream rather than folded into
score_prospect below -- scoring it again would double-count the same fact.

Field reality check (verified against a live search, not assumed): Apollo's
`mixed_people/api_search` returns exactly two free-text fields per person --
`title` and `organization.name` -- plus a handful of boolean presence-flags
(`has_city`, `has_state`, `has_country`, `organization.has_industry`,
`organization.has_revenue`, `organization.has_employee_count`, etc.).
There is no industry name, company description, employee count *value*,
funding stage, headline, or location *value* in this response -- only
whether Apollo has that data on file, not what it says. Scoring below uses
every field that carries real signal; fields that are presence-only get a
small tie-breaker weight, and fields with no signal at all (phone presence,
IDs, obfuscated names) aren't used.

Every weight lives in the *_WEIGHTS dicts below so tuning never requires
touching scoring logic. Set DEBUG = True to log a per-prospect breakdown
(also available on demand via explain_score(), independent of the flag).

Status: the deliberate seam mentioned above has been filled -- see
prospect_ranker.py. Claude now does the actual quality ranking (holistic
judgment, not keyword matching), so score_prospect()/rank_prospects()/
select_best() are NOT called by the active pipeline anymore. They're kept
here rather than deleted -- they're real, tested, zero-cost, zero-latency
functionality that's still useful as a fallback or for comparing
deterministic vs. LLM ranking quality later. The pipeline now uses
select_pool() instead: partition by email only, no scoring, since scoring
this pool is Claude's job.
"""

from loguru import logger

# Toggle to log a score breakdown for every prospect scored during a real
# selection run. Independent of this flag, explain_score() below always
# computes and returns a breakdown on demand (used for debugging/inspection
# without turning on logging for a whole run).
DEBUG = False


class ProspectSelector:
    # Checked against `title`, lowercased. Matches take the SINGLE highest
    # weight found (seniority is one classification, not stackable) --
    # e.g. "Partner, Investor Relations" scores as "partner" (2.0), not
    # "partner" + "investor" (3.0).
    TITLE_WEIGHTS = {
        "managing partner": 3.0,
        "general partner": 3.0,
        "founder": 3.0,
        "co-founder": 3.0,
        "chief investment officer": 3.0,
        "cio": 3.0,
        "investment director": 3.0,
        "partner": 2.0,
        "principal": 2.0,
        "managing director": 2.0,
        "venture partner": 1.0,
        "director": 1.0,
        "investor": 1.0,
        "associate": 1.0,
    }

    # Checked against `organization.name`, lowercased. Also a max-match --
    # a firm is one type, not a sum of every keyword it happens to contain.
    FIRM_WEIGHTS = {
        "venture capital": 2.0,
        "venture partners": 2.0,
        "family office": 2.0,
        "private equity": 2.0,
        "corporate venture": 2.0,
        "ventures": 1.5,
        "capital": 1.0,
        "investments": 1.0,
        "investment": 1.0,
    }

    # Checked against `title` + `organization.name` combined -- these are
    # role/behavior signals that can show up in either field (e.g. "Angel
    # Investor" is usually a title, not an org name), and unlike the two
    # tables above, distinct matches ARE additive: being both a board
    # member and an accelerator partner is genuinely two separate signals.
    KEYWORD_WEIGHTS = {
        "angel": 1.0,
        "board": 0.5,
        "accelerator": 0.5,
        "scout": 0.5,
        "limited partner": 0.5,
        "advisor": 0.25,
        "serial": 0.25,  # weak proxy for repeat investor / founder experience
        "exited": 0.5,  # exited founder, on the rare occasion it's stated
    }

    # Apollo's fallback category label when it doesn't have a firm's actual
    # name -- a signal of a sparser record, not a real (possibly large,
    # generic) company. A specifically-named firm beats this.
    GENERIC_ORG_NAMES = {"venture capital", "private equity", "investment management"}
    SPECIFIC_FIRM_NAME_BONUS = 1.0

    # Presence-flag tie-breakers below. These fields carry NO value, only
    # "does Apollo have this on file" -- kept intentionally small so a
    # sparse-but-senior prospect never loses to a complete-but-junior one.
    INDUSTRY_WEIGHTS = {"has_industry": 0.15}
    LOCATION_WEIGHTS = {"has_city": 0.05, "has_state": 0.05, "has_country": 0.05}
    COMPANY_WEIGHTS = {"has_revenue": 0.1, "has_employee_count": 0.1}

    @staticmethod
    def partition_by_email(people: list[dict]) -> tuple[list[dict], list[dict]]:
        """Splits into (has_email, no_email), preserving Apollo's original order."""
        with_email = [p for p in people if p.get("has_email")]
        without_email = [p for p in people if not p.get("has_email")]
        return with_email, without_email

    @classmethod
    def select_pool(cls, people: list[dict], pool_size: int) -> dict:
        """
        Assembles the candidate pool for Claude to rank -- email-priority
        partition only, deliberately UNSCORED (Apollo's own relevance order
        is preserved within each group). Quality ranking within this pool
        is ProspectRanker's job, not this method's.
        """
        with_email, without_email = cls.partition_by_email(people)

        pool = with_email[:pool_size]
        if len(pool) < pool_size:
            pool = pool + without_email[: pool_size - len(pool)]

        return {
            "pool": pool,
            "prospects_with_email": len(with_email),
            "prospects_without_email": len(without_email),
        }

    @staticmethod
    def _best_match(text: str, weights: dict[str, float]) -> tuple[float, str | None]:
        """Highest weight among keys in `weights` found as a substring of `text`."""
        best_weight, best_keyword = 0.0, None
        for keyword, weight in weights.items():
            if keyword in text and weight > best_weight:
                best_weight, best_keyword = weight, keyword
        return best_weight, best_keyword

    @staticmethod
    def _sum_matches(text: str, weights: dict[str, float]) -> list[tuple[str, float]]:
        """Every distinct key in `weights` found in `text`, each counted once."""
        return [(keyword, weight) for keyword, weight in weights.items() if keyword in text]

    @classmethod
    def _score_with_breakdown(cls, person: dict) -> tuple[float, list[tuple[str, float]]]:
        title = (person.get("title") or "").lower()
        org = person.get("organization") or {}
        org_name = (org.get("name") or "").lower()
        combined_text = f"{title} {org_name}"

        breakdown: list[tuple[str, float]] = []

        title_weight, title_kw = cls._best_match(title, cls.TITLE_WEIGHTS)
        if title_weight:
            breakdown.append((f"Title: '{title_kw}'", title_weight))

        firm_weight, firm_kw = cls._best_match(org_name, cls.FIRM_WEIGHTS)
        if firm_weight:
            breakdown.append((f"Firm type: '{firm_kw}'", firm_weight))

        for keyword, weight in cls._sum_matches(combined_text, cls.KEYWORD_WEIGHTS):
            breakdown.append((f"Keyword: '{keyword}'", weight))

        if org_name and org_name not in cls.GENERIC_ORG_NAMES:
            breakdown.append(("Specific firm name (not generic)", cls.SPECIFIC_FIRM_NAME_BONUS))

        if org.get("has_industry"):
            breakdown.append(("Has industry data", cls.INDUSTRY_WEIGHTS["has_industry"]))

        for flag, weight in cls.LOCATION_WEIGHTS.items():
            if person.get(flag):
                breakdown.append((f"Has {flag.replace('has_', '')}", weight))

        for flag, weight in cls.COMPANY_WEIGHTS.items():
            if org.get(flag):
                breakdown.append((f"Company: {flag.replace('_', ' ')}", weight))

        score = sum(weight for _, weight in breakdown)
        return score, breakdown

    @classmethod
    def explain_score(cls, person: dict) -> dict:
        """
        On-demand score + breakdown for a single prospect, independent of
        the DEBUG flag -- for debugging tools / inspection scripts.
        """
        score, breakdown = cls._score_with_breakdown(person)
        return {"score": score, "breakdown": breakdown}

    @classmethod
    def score_prospect(cls, person: dict) -> float:
        """
        Higher is better. Deterministic and fully explainable from fields
        already present in the search response -- no field this function
        reads required an extra Apollo call or enrichment credit.
        """
        score, breakdown = cls._score_with_breakdown(person)
        if DEBUG:
            org_name = (person.get("organization") or {}).get("name")
            lines = [f"{person.get('title')} @ {org_name} -- Score: {score:.2f}"]
            lines += [f"  {weight:+.2f} {label}" for label, weight in breakdown]
            logger.debug("\n".join(lines))
        return score

    @classmethod
    def rank_prospects(cls, people: list[dict]) -> list[dict]:
        """Best first. Stable sort -- ties keep Apollo's own relevance order."""
        return sorted(people, key=cls.score_prospect, reverse=True)

    @classmethod
    def select_best(cls, people: list[dict], target_count: int) -> dict:
        """
        Selects up to target_count prospects: all available with-email
        prospects (ranked, best first) before any email-less ones, which
        only fill remaining slots -- also ranked, not taken arbitrarily.
        """
        with_email, without_email = cls.partition_by_email(people)
        ranked_with_email = cls.rank_prospects(with_email)
        ranked_without_email = cls.rank_prospects(without_email)

        selected = ranked_with_email[:target_count]
        if len(selected) < target_count:
            selected = selected + ranked_without_email[: target_count - len(selected)]

        return {
            "selected": selected,
            "prospects_with_email": len(with_email),
            "prospects_without_email": len(without_email),
        }
