"""
Shared write-transaction guard for the SQLite*Store classes.

Every store's create()/save()/update()/delete() does a single execute()
immediately followed by commit(). Before this helper, a caught exception
(most commonly aiosqlite.IntegrityError from a UNIQUE violation -- an
expected, routine condition for these stores' own duplicate-detection
columns) was re-raised WITHOUT ever calling rollback(), leaving that
connection's transaction open indefinitely. Because each store's connection
is opened once at app startup and reused for the process's whole life, the
next statement on that same connection then runs against a stale WAL
snapshot: SQLite raises SQLITE_BUSY_SNAPSHOT ("database is locked")
instantly and does NOT retry it via the busy handler -- busy_timeout has no
effect on this failure mode, since waiting cannot fix a stale snapshot.
Worse, while the transaction sits open, it also holds the file's one write
lock, so every other store's writes (any table, same file) fail/block too,
until this connection's own next statement finally succeeds. Wrapping the
execute() in this context manager guarantees rollback() runs on ANY
exception, so a connection can never be left in this state.
"""

from contextlib import asynccontextmanager
from typing import AsyncIterator

import aiosqlite


@asynccontextmanager
async def sqlite_write(conn: aiosqlite.Connection) -> AsyncIterator[None]:
    try:
        yield
    except BaseException:
        await conn.rollback()
        raise
    else:
        await conn.commit()
