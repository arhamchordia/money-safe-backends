"""Database helpers shared by every part of the lab.

MoneySafe uses SQLite: it ships with Python and runs inside any container
with no setup. SQLite allows one writer at a time, which makes locking easy
to see. Each lesson notes the Postgres equivalent.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from importlib import resources
from itertools import count
from typing import Iterator

DEFAULT_DB = "moneysafe.db"
_savepoint_ids = count(1)


def db_path() -> str:
    return os.environ.get("MONEYSAFE_DB", DEFAULT_DB)


def connect(path: str | None = None) -> sqlite3.Connection:
    """Open a connection configured for safe concurrent use.

    isolation_level=None  -> we control transactions ourselves (no hidden BEGIN).
    timeout=10            -> wait up to 10 s for the write lock instead of failing.
    WAL journal mode      -> readers never block the single writer.
    foreign_keys=ON       -> SQLite leaves these off unless asked.
    """
    conn = sqlite3.connect(path or db_path(), timeout=10, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block atomically.

    Outermost call: BEGIN IMMEDIATE takes SQLite's write lock up front, so no
    other writer can sneak in between our reads and our writes.
    (Postgres: BEGIN, then SELECT ... FOR UPDATE on the rows you will change.)

    Nested call: a SAVEPOINT, so helpers can be composed inside one transaction.
    """
    if conn.in_transaction:
        name = f"sp_{next(_savepoint_ids)}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except BaseException:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
            raise
        else:
            conn.execute(f"RELEASE {name}")
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def init_db(conn: sqlite3.Connection) -> None:
    """Create every table, trigger and system account. Safe to run twice."""
    schema = resources.files("moneysafe").joinpath("schema.sql").read_text()
    conn.executescript(schema)
