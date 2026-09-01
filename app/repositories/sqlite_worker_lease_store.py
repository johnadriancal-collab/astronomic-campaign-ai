"""SQLite-backed WorkerLeaseStore -- lease_name is the literal PRIMARY KEY.

try_acquire() uses `INSERT ... ON CONFLICT DO UPDATE ... WHERE
worker_leases.expires_at <= ?` -- SQLite's conditional-UPSERT form. This
was verified empirically (not assumed) before relying on it: SQLite
performs the UPDATE arm ONLY when the WHERE clause is true, leaves the
existing row completely untouched and reports zero changed rows otherwise
-- exactly the CAS semantics "acquire iff missing or expired" needs in one
atomic statement, with no separate SELECT-then-INSERT/UPDATE race window.
"""

from datetime import datetime, timedelta

import aiosqlite

from app.models.worker_lease import WorkerLease
from app.repositories.sqlite_connection import open_sqlite_connection
from app.repositories.sqlite_txn import sqlite_write
from app.repositories.worker_lease_store import WorkerLeaseStore

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS worker_leases (
    lease_name TEXT PRIMARY KEY,
    holder_id TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


class SQLiteWorkerLeaseStore(WorkerLeaseStore):
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await open_sqlite_connection(self._db_path)
        await self._conn.execute(CREATE_TABLE_SQL)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SQLiteWorkerLeaseStore.connect() must be called before use")
        return self._conn

    async def try_acquire(self, lease_name: str, holder_id: str, now: datetime, lease_duration_seconds: int) -> bool:
        expires_at = now + timedelta(seconds=lease_duration_seconds)
        async with sqlite_write(self._connection):
            cursor = await self._connection.execute(
                "INSERT INTO worker_leases (lease_name, holder_id, acquired_at, expires_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (lease_name) DO UPDATE SET "
                "  holder_id = excluded.holder_id, acquired_at = excluded.acquired_at, "
                "  expires_at = excluded.expires_at, updated_at = excluded.updated_at "
                "WHERE worker_leases.expires_at <= ?",
                (lease_name, holder_id, now.isoformat(), expires_at.isoformat(), now.isoformat(), now.isoformat()),
            )
        return cursor.rowcount > 0

    async def try_renew(self, lease_name: str, holder_id: str, now: datetime, lease_duration_seconds: int) -> bool:
        expires_at = now + timedelta(seconds=lease_duration_seconds)
        async with sqlite_write(self._connection):
            cursor = await self._connection.execute(
                "UPDATE worker_leases SET expires_at = ?, updated_at = ? WHERE lease_name = ? AND holder_id = ?",
                (expires_at.isoformat(), now.isoformat(), lease_name, holder_id),
            )
        return cursor.rowcount > 0

    async def get(self, lease_name: str) -> WorkerLease | None:
        cursor = await self._connection.execute(
            "SELECT holder_id, acquired_at, expires_at, updated_at FROM worker_leases WHERE lease_name = ?",
            (lease_name,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return WorkerLease(
            lease_name=lease_name,
            holder_id=row["holder_id"],
            acquired_at=row["acquired_at"],
            expires_at=row["expires_at"],
            updated_at=row["updated_at"],
        )

    async def release(self, lease_name: str, holder_id: str) -> bool:
        async with sqlite_write(self._connection):
            cursor = await self._connection.execute(
                "DELETE FROM worker_leases WHERE lease_name = ? AND holder_id = ?", (lease_name, holder_id)
            )
        return cursor.rowcount > 0
