"""
A real app.main lifespan startup smoke test.

Every other test file in this repo builds an isolated mini FastAPI app
mounting only the router(s) it needs (see e.g. test_activity_api.py) --
none of them ever import and start the REAL app.main.app / its real
lifespan(). That's normally fine (it keeps each test file fast and
focused), but it means a NameError or other startup-time bug in
app/main.py's lifespan function has no test coverage at all -- exactly
what happened during Astro AI Phase 3's rollout (AstroCampaignTools was
wired using bare local names like `campaign_service` that were never
actually assigned as local variables, only as `app.state.campaign_service`
-- a real production startup crash that 1325 other passing tests never
caught, since none of them exercise this code path).

This file closes that specific gap: it actually enters app.main's real
lifespan context manager, the same as a real deployment does, and fails
loudly if startup raises anything.
"""

import pytest

from app.main import app, lifespan

pytestmark = pytest.mark.asyncio


async def test_real_app_lifespan_starts_up_without_error():
    async with lifespan(app):
        pass
