"""
FastAPI entry point for Astronomic Campaign AI.

Endpoints:
    GET  /health                        - health check
    POST /campaign/preview              - Claude plan only, no Apollo calls
    POST /campaign/search                - Apollo prospect search using a generated plan's filters
    POST /campaign                       - builds list/sequence/steps/enrollment (does NOT send unless auto_launch=true)
    POST /campaign/launch/{sequence_id}  - explicit human-confirmed activation
"""

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from loguru import logger

from app.models.campaign import CampaignExecutionReport, CampaignPlan, CampaignRequest
from app.services.campaign_service import CampaignService

app = FastAPI(title="Astronomic Campaign AI")
service = CampaignService()

HOMEPAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Astronomic Campaign AI</title>
<style>
  body { font-family: -apple-system, system-ui, sans-serif; max-width: 640px; margin: 4rem auto; padding: 0 1.5rem; color: #1a1a1a; }
  h1 { font-size: 1.4rem; }
  .status { display: inline-flex; align-items: center; gap: 0.5rem; font-size: 0.95rem; margin: 1rem 0 2rem; }
  .dot { width: 0.6rem; height: 0.6rem; border-radius: 50%; background: #999; }
  .dot.ok { background: #2ea043; }
  .dot.err { background: #d1242f; }
  ul { line-height: 1.9; padding-left: 1.2rem; }
  code { background: #f0f0f0; padding: 0.1rem 0.35rem; border-radius: 4px; font-size: 0.9em; }
</style>
</head>
<body>
<h1>Astronomic Campaign AI</h1>
<div class="status"><span class="dot" id="dot"></span><span id="status-text">Checking server status…</span></div>
<ul>
  <li><a href="/docs">Swagger UI</a> — interactive API docs, try endpoints from the browser</li>
  <li><a href="/redoc">ReDoc</a> — read-only API reference</li>
  <li><a href="/health">/health</a> — raw health-check JSON</li>
</ul>
<p>No dedicated frontend yet — <code>POST /campaign/preview</code>, <code>/campaign/search</code>, and <code>/campaign</code> are exercised via the Swagger UI or <code>curl</code> for now.</p>
<script>
fetch("/health").then(r => r.json()).then(d => {
  document.getElementById("dot").classList.add(d.status === "ok" ? "ok" : "err");
  document.getElementById("status-text").textContent = d.status === "ok" ? "Server is running" : "Server responded with an error";
}).catch(() => {
  document.getElementById("dot").classList.add("err");
  document.getElementById("status-text").textContent = "Could not reach /health";
});
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def home():
    return HOMEPAGE_HTML


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/campaign/preview", response_model=CampaignPlan)
async def preview_campaign(req: CampaignRequest):
    """Returns Claude's generated campaign plan only. No Apollo side effects."""
    try:
        return await service.preview(req.prompt)
    except Exception as e:
        logger.error(f"Preview failed: {e}")
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/campaign/search")
async def search_prospects(req: CampaignRequest):
    """Generates a plan, then returns matching Apollo prospects (no writes)."""
    try:
        plan = await service.preview(req.prompt)
        results = await service.apollo.search_people(plan.filters.model_dump())
        return results
    except Exception as e:
        logger.error(f"Search failed: {e}")
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/campaign")
async def create_campaign(req: CampaignRequest, auto_launch: bool = False):
    """
    Builds the full campaign: searches prospects, creates the list and
    sequence, adds steps, and enrolls contacts.

    By default this does NOT send any emails, regardless of what Claude's
    plan sets for `launch` — pass auto_launch=true explicitly, or call
    /campaign/launch/{sequence_id} once you've reviewed the report.
    """
    try:
        plan, report = await service.build_campaign(req.prompt, auto_launch=auto_launch)
        return {"plan": plan, "report": report}
    except Exception as e:
        logger.error(f"Campaign creation failed: {e}")
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/campaign/launch/{sequence_id}", response_model=CampaignExecutionReport)
async def launch_campaign(sequence_id: str):
    """Explicitly activates an already-built sequence, sending it live."""
    try:
        return await service.launch(sequence_id)
    except Exception as e:
        logger.error(f"Launch failed: {e}")
        raise HTTPException(status_code=502, detail=str(e))
