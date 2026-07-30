"""
Prospect search against Apollo's people/company database.
"""

from app.apollo.client import ApolloBaseClient


class PeopleClient(ApolloBaseClient):
    async def search_people(self, filters: dict, page: int = 1, per_page: int = 25) -> dict:
        payload: dict = {"page": page, "per_page": per_page}

        if filters.get("titles"):
            payload["person_titles"] = filters["titles"]
        if filters.get("locations"):
            payload["person_locations"] = filters["locations"]
        if filters.get("industries"):
            # organization_industries isn't a real people-search param; Apollo's
            # closest current equivalent is free-text keyword tags, not a fixed
            # industry taxonomy.
            payload["q_organization_keyword_tags"] = filters["industries"]
        if filters.get("company_size"):
            payload["organization_num_employees_ranges"] = filters["company_size"]
        # funding_stage is intentionally dropped here: Apollo's people-search has
        # no categorical funding-stage param (organization_latest_funding_stage_cd
        # doesn't exist). It only exposes numeric/date funding ranges
        # (latest_funding_amount_range, total_funding_range, latest_funding_date_range),
        # which don't match categorical values like "Series A" -- needs a follow-up
        # to map stage names to ranges or drop the field upstream.

        return await self.request("POST", "/mixed_people/api_search", json=payload)

    async def search_companies(self, filters: dict, page: int = 1, per_page: int = 25) -> dict:
        payload: dict = {"page": page, "per_page": per_page}

        if filters.get("industries"):
            payload["organization_industries"] = filters["industries"]
        if filters.get("locations"):
            payload["organization_locations"] = filters["locations"]
        if filters.get("funding_stage"):
            payload["organization_latest_funding_stage_cd"] = filters["funding_stage"]

        return await self.request("POST", "/mixed_companies/search", json=payload)
