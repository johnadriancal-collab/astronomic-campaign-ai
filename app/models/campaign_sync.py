"""
The result of one CampaignSyncService.sync() run -- returned to the
caller for visibility/debugging, per explicit design requirement. Not
stored anywhere; a fresh report is produced by every sync call.
"""

from pydantic import BaseModel


class CampaignSyncReport(BaseModel):
    found: int  # total sequences returned by Apollo this run (matches its own total_entries)
    created: int  # new local Campaign+EmailSequence records created
    updated: int  # existing SYNCED records where something actually changed
    archived: int  # existing records newly confirmed archived/deleted this run
    unchanged: int  # existing SYNCED records where nothing differed from what we already had
    duration_ms: float
