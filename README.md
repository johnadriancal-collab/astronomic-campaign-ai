# Astronomic Campaign AI

Describe an outreach campaign in plain English → Claude drafts the targeting
filters and email sequence → the backend builds it in Apollo (list, sequence,
steps, enrollment) for review before anything sends.

Two parts:

- **`app/`** — FastAPI backend. Calls the Anthropic API to generate a
  campaign plan, then calls Apollo's API to search prospects and (optionally)
  build out the list/sequence/steps.
- **`frontend/`** — Next.js UI. A landing page to describe a campaign and a
  results page showing the generated plan plus live Apollo search/build
  progress.

See [ROADMAP.md](ROADMAP.md) for exactly which Apollo integrations have been
verified against the real API versus which are still known-broken or
unimplemented — several endpoint names in Apollo's public API differ from
what their docs suggest, and that file tracks the ones already found and
fixed.

## Local development

### Backend

Requires Python 3.10+ (uses `X | None` union syntax). [uv](https://astral.sh/uv)
is the easiest way to get a matching interpreter and manage the venv.

```bash
uv venv --python 3.12
uv pip install -r requirements.txt
cp .env.example .env   # fill in ANTHROPIC_API_KEY and APOLLO_API_KEY
uv run uvicorn app.main:app --reload
```

Runs on `http://localhost:8000`. Visit `/docs` for interactive Swagger UI.

### Frontend

Requires Node.js 20+.

```bash
cd frontend
npm install
npm run dev
```

Runs on `http://localhost:3000` and proxies backend calls through
`/backend/*` (see `next.config.ts`) to `http://localhost:8000` by default —
no CORS setup needed locally. To point it at a different backend, set
`BACKEND_ORIGIN` (see [Deployment](#deployment) below).

### Running both

Start the backend first, then the frontend — the frontend's proxy expects
the backend to already be reachable at `BACKEND_ORIGIN`.

## Endpoints

| Method | Path | What it does |
|---|---|---|
| GET | `/health` | Health check |
| POST | `/campaign/preview` | Claude's plan only — no Apollo calls |
| POST | `/campaign/search` | Generates a plan, returns matching Apollo prospects (no writes) |
| POST | `/campaign` | Builds list + sequence + steps + enrollment. **Does not send** unless `?auto_launch=true` |
| POST | `/campaign/launch/{sequence_id}` | Explicitly activates a sequence that's already built |

Example:

```bash
curl -X POST localhost:8000/campaign/preview \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Create an investor dinner campaign for FlexRadio. Location: San Francisco. Audience: early-stage technology investors. Sequence: 4 emails. Tone: professional, conversational, not salesy. Delay: 3 days between emails."}'
```

## Deployment

### Backend → Railway

The root `Dockerfile` builds and runs the FastAPI app as-is — Railway can
deploy directly from it with no extra config. Set these environment
variables in the Railway project settings (never commit them):

| Variable | Required | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes | |
| `APOLLO_API_KEY` | yes | |
| `CLAUDE_MODEL` | no | defaults to `claude-sonnet-4-5` |
| `APOLLO_BASE_URL` | no | defaults to `https://api.apollo.io/api/v1` |
| `DEFAULT_SENDER_MAILBOX_ID` | no | only needed for sequence enrollment |

Railway assigns a public URL (e.g. `https://<service>.up.railway.app`) —
that's the backend origin the frontend needs.

### Frontend → Vercel

Import the repo, set the **root directory to `frontend/`**, and set:

| Variable | Required | Notes |
|---|---|---|
| `BACKEND_ORIGIN` | yes (in production) | the Railway backend URL, no trailing slash. Server-side only — never sent to the browser. |

No API keys are ever needed on the frontend; it only ever talks to its own
backend proxy route.

## Design choices worth knowing about

- **Launch is never automatic from Claude's output.** The `launch` field in
  Claude's JSON is informational only. Sending only happens via an explicit
  `auto_launch=true` on `/campaign`, or a separate `/campaign/launch` call —
  keeping a human in the loop before real emails go to real investors.
- **Never-fail-silently**: every Apollo call failure is caught and logged
  into the execution report rather than aborting the whole run, so partial
  failures (e.g. one bad contact) are visible instead of hidden.
- **Client errors don't retry**: 4xx responses from Apollo (bad request,
  auth issues) fail immediately rather than retrying, since retrying won't
  fix a malformed request or bad key. 5xx and network errors do retry.
- **No CORS configuration on the backend.** The frontend never calls it
  cross-origin — `frontend/next.config.ts` rewrites `/backend/*` to
  `BACKEND_ORIGIN` server-side, so the browser only ever talks to the
  frontend's own origin.

## Future agents

The `agents/` and `services/` structure is meant to make it straightforward
to add more agents alongside `CampaignAgent` — e.g. an ITF-reading agent, a
CRM sync agent, a reporting agent, or a Slack-notification agent — each
following the same pattern: a focused agent class, a service that
orchestrates it, and a thin FastAPI route.
