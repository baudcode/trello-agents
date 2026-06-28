"""SQLite schema, migrations, connection helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import aiosqlite


async def get_connection(db_path: Path) -> aiosqlite.Connection:
    """Open a connection with WAL mode enabled."""
    conn = await aiosqlite.connect(str(db_path))
    await conn.execute("PRAGMA journal_mode=WAL")
    return conn


async def run_migrations(conn: aiosqlite.Connection) -> None:
    """Create or upgrade all tables to the multi-project schema."""
    await conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS cards (
            task_id TEXT PRIMARY KEY,
            short_id TEXT,
            current_column TEXT,
            branch_name TEXT,
            worktree_path TEXT,
            agent_status TEXT,
            last_review_comment_id TEXT,
            updated_at TEXT,
            board_id TEXT
        );

        CREATE TABLE IF NOT EXISTS processed_actions (
            action_id TEXT PRIMARY KEY,
            processed_at TEXT,
            board_id TEXT
        );

        CREATE TABLE IF NOT EXISTS webhook_cursor (
            board_id TEXT PRIMARY KEY,
            cursor TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS event_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_json TEXT,
            processed_at TEXT,
            error TEXT,
            board_id TEXT
        );

        CREATE TABLE IF NOT EXISTS card_locks (
            task_id TEXT PRIMARY KEY,
            locked_at TEXT,
            board_id TEXT
        );
        """
    )
    await conn.commit()

    # Idempotent column additions for databases created on the pre-multi-project schema.
    await _add_column_if_missing(conn, "cards", "board_id", "TEXT")
    await _add_column_if_missing(conn, "processed_actions", "board_id", "TEXT")
    await _add_column_if_missing(conn, "event_log", "board_id", "TEXT")
    await _add_column_if_missing(conn, "card_locks", "board_id", "TEXT")

    # webhook_cursor was previously a singleton row (PK id=1). If the legacy
    # schema is in place, migrate it to (board_id PK, cursor, updated_at).
    await _migrate_webhook_cursor(conn)

    await conn.commit()


async def _add_column_if_missing(
    conn: aiosqlite.Connection, table: str, column: str, decl: str
) -> None:
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    existing = {row[1] for row in rows}
    if column not in existing:
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


async def _migrate_webhook_cursor(conn: aiosqlite.Connection) -> None:
    cursor = await conn.execute("PRAGMA table_info(webhook_cursor)")
    rows = await cursor.fetchall()
    cols = {row[1] for row in rows}
    if "board_id" in cols:
        return
    # Legacy: id PRIMARY KEY DEFAULT 1, cursor TEXT, updated_at TEXT
    # Read the single existing row (if any) and rebuild the table.
    legacy_cursor: str | None = None
    legacy_updated: str | None = None
    if "id" in cols and "cursor" in cols:
        legacy = await conn.execute("SELECT cursor, updated_at FROM webhook_cursor WHERE id = 1")
        row = await legacy.fetchone()
        if row is not None:
            legacy_cursor, legacy_updated = row[0], row[1]
    await conn.execute("DROP TABLE webhook_cursor")
    await conn.execute(
        "CREATE TABLE webhook_cursor (board_id TEXT PRIMARY KEY, cursor TEXT, updated_at TEXT)"
    )
    if legacy_cursor is not None:
        await conn.execute(
            "INSERT INTO webhook_cursor (board_id, cursor, updated_at) VALUES (?, ?, ?)",
            ("__legacy__", legacy_cursor, legacy_updated or datetime.now(UTC).isoformat()),
        )


async def mark_action_processed(
    conn: aiosqlite.Connection, action_id: str, board_id: str | None = None
) -> None:
    """Record an action as processed."""
    now = datetime.now(UTC).isoformat()
    await conn.execute(
        "INSERT OR IGNORE INTO processed_actions (action_id, processed_at, board_id) "
        "VALUES (?, ?, ?)",
        (action_id, now, board_id),
    )
    await conn.commit()


async def is_action_processed(conn: aiosqlite.Connection, action_id: str) -> bool:
    """Check whether an action has already been processed.

    Trello action IDs are globally unique, so board_id filtering is not required
    for correctness.
    """
    cursor = await conn.execute(
        "SELECT 1 FROM processed_actions WHERE action_id = ?",
        (action_id,),
    )
    row = await cursor.fetchone()
    return row is not None


async def log_event(
    conn: aiosqlite.Connection,
    event_json: str,
    error: str | None = None,
    board_id: str | None = None,
) -> None:
    """Append an entry to the event log."""
    now = datetime.now(UTC).isoformat()
    await conn.execute(
        "INSERT INTO event_log (event_json, processed_at, error, board_id) VALUES (?, ?, ?, ?)",
        (event_json, now, error, board_id),
    )
    await conn.commit()


async def get_cursor(conn: aiosqlite.Connection, board_id: str) -> str | None:
    """Return the current webhook cursor for a board, or None if not set."""
    cursor = await conn.execute("SELECT cursor FROM webhook_cursor WHERE board_id = ?", (board_id,))
    row = await cursor.fetchone()
    if row is None:
        return None
    return row[0]


async def set_cursor(conn: aiosqlite.Connection, cursor: str, board_id: str) -> None:
    """Upsert the webhook cursor for a board."""
    now = datetime.now(UTC).isoformat()
    await conn.execute(
        "INSERT INTO webhook_cursor (board_id, cursor, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(board_id) DO UPDATE SET cursor = excluded.cursor, "
        "updated_at = excluded.updated_at",
        (board_id, cursor, now),
    )
    await conn.commit()
