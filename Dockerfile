FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Only the specific, git-tracked operator CLI(s) actually needed at
# runtime -- deliberately NOT `COPY scripts ./scripts` (whole directory).
# scripts/ also holds ad-hoc, untracked, local-file-input analysis tools
# (e.g. audit_contact_names.py) that are never meant to run inside this
# container and were never reviewed for production packaging; a
# directory-wide copy would silently bundle whatever untracked files
# happen to be sitting in scripts/ at build time. Add one explicit COPY
# line per operator script as new ones are approved for in-container use.
COPY scripts/run_luma_engagement_participant_backfill.py ./scripts/run_luma_engagement_participant_backfill.py

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
