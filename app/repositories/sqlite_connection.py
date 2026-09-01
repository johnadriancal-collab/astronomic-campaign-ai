"""
Shared SQLite connection-factory for the SQLite*Store classes.

Every store already independently opens its own aiosqlite.Connection once
at app startup and holds it for the process's whole life (see
sqlite_txn.py's docstring for why that lifecycle matters). Every store's
connect() also does the identical `PRAGMA journal_mode=WAL` line. This
module centralizes THAT PART -- the connection-opening/PRAGMA CONFIGURATION
-- into one function, so there is exactly one place to tune it (e.g. add
another PRAGMA later), without restructuring any store's own class/
connection-lifecycle (which stays exactly as it is: each store still owns
its own connect()/close(), still calls this once, still holds the result).

Deliberately a plain function, not a shared base class: 28 stores already
independently implement this identical connect() shape, and refactoring
their inheritance hierarchy to share a base class would be a much larger,
riskier change to already-shipped, already-tested code for the same
benefit a small, additive, per-file-mechanical helper already provides.
Each store's connect() replaces its own `aiosqlite.connect(...)` +
`PRAGMA journal_mode=WAL` lines with one call to open_sqlite_connection() --
a small, independently-revertible diff per file, no behavior change beyond
adding the busy_timeout below.

`PRAGMA busy_timeout=5000` is the one new behavior this introduces (not
previously set anywhere in this codebase -- see sqlite_txn.py's own
docstring: the historical locking incident was a stale-WAL-snapshot issue
busy_timeout would NOT have fixed, but that doesn't mean busy_timeout has
no value going forward -- it's specifically what protects against a
GENUINE, ordinary write collision between two different processes writing
to the same file, e.g. the future Phase C worker process and the web
process, which is a real, new scenario this app has never had before now).
Without it, SQLite returns SQLITE_BUSY almost immediately on a transient
write collision instead of waiting a bounded amount of time and retrying,
which is much more likely to surface as a spurious, avoidable error once a
second writing process exists. 5000ms is a starting value, not derived from
any measurement -- see the Phase A specification's "decisions needing
sign-off" list.
"""

from pathlib import Path

import aiosqlite

BUSY_TIMEOUT_MS = 5000


async def open_sqlite_connection(db_path: str) -> aiosqlite.Connection:
    """Opens (creating the parent directory if needed), configures
    (row_factory + WAL + busy_timeout), and returns a single
    aiosqlite.Connection -- callers own its lifecycle from here exactly as
    before (hold it, close() it at shutdown); this function itself does not
    create the caller's tables/indexes, since those are store-specific."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return conn
