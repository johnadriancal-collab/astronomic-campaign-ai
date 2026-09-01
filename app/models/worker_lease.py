"""
WorkerLease -- Astronomic Mail Phase C. The single-writer leadership
mechanism for the campaign-execution worker (see app/services/
worker_lease_service.py and the Phase C investigation's own reasoning:
Railway exposes RAILWAY_REPLICA_ID for logging but no reliable "how many
replicas currently exist" signal a running process can inspect, so the
authoritative guard has to live at the database level, not in
configuration alone).

One row per lease NAME (`lease_name` is the primary key) -- this app only
ever has one lease ("mail_execution_worker") today, but the shape allows a
second, independent lease later without a schema change. Whoever currently
holds a valid (non-expired) lease is the only process permitted to claim
new execution work or invoke a provider -- see MailSendingService's
controlled-test-gate-adjacent leadership check.
"""

from datetime import datetime

from pydantic import BaseModel


class WorkerLease(BaseModel):
    lease_name: str
    holder_id: str
    acquired_at: datetime
    expires_at: datetime
    updated_at: datetime
