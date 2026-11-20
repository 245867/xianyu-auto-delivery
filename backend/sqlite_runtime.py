"""Uniform SQLite connection settings for concurrent local workers."""

from __future__ import annotations

import os
import sqlite3
from typing import Union

BUSY_TIMEOUT_MS = 30_000

DatabasePath = Union[str, os.PathLike[str]]


def connect_sqlite(
    database: DatabasePath,
    *,
    timeout: float = 30.0,
    check_same_thread: bool = True,
    isolation_level: str | None = "",
) -> sqlite3.Connection:
    """Open SQLite with WAL and bounded waiting for file-backed databases."""
    database_path = os.fspath(database)
    conn = sqlite3.connect(
        database_path,
        timeout=timeout,
        check_same_thread=check_same_thread,
        isolation_level=isolation_level,
    )
    try:
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        if database_path != ":memory:":
            journal_mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()
            enabled_mode = str(journal_mode[0]).lower() if journal_mode else ""
            if enabled_mode != "wal":
                raise sqlite3.DatabaseError(
                    f"WAL mode could not be enabled for SQLite database "
                    f"{database_path!r}: {enabled_mode or 'unknown'}"
                )
        conn.execute("PRAGMA synchronous = NORMAL")
    except Exception:
        conn.close()
        raise
    return conn
