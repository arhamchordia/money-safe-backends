"""Database helpers shared by every part of the lab.

MoneySafe runs on PostgreSQL 16 through psycopg 3.

Connection string: the MONEYSAFE_DSN environment variable, for example
    postgresql://app@localhost/moneysafe
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from importlib import resources
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

DEFAULT_DSN = "postgresql:///moneysafe"

Connection = psycopg.Connection


def dsn() -> str:
    return os.environ.get("MONEYSAFE_DSN", DEFAULT_DSN)


def connect(conninfo: str | None = None) -> Connection:
    """Open a connection.

    autocommit=True   -> nothing runs inside a hidden transaction; we open
                         transactions explicitly with `transaction()`.
    dict_row          -> rows come back as dicts: row["balance_paise"].
    """
    return psycopg.connect(conninfo or dsn(), autocommit=True, row_factory=dict_row)


@contextmanager
def transaction(conn: Connection) -> Iterator[Connection]:
    """Run a block atomically: everything commits together or nothing does.

    Outermost call: BEGIN ... COMMIT (or ROLLBACK on any exception).
    Nested call: a SAVEPOINT, so helpers can be composed inside one transaction.

    Locks are taken explicitly inside, with SELECT ... FOR UPDATE on the rows
    you are about to change. Postgres holds them until the transaction ends.
    """
    with conn.transaction():
        yield conn


def init_db(conn: Connection) -> None:
    """Create every table, trigger and system account. Safe to run twice."""
    schema = resources.files("moneysafe").joinpath("schema.sql").read_text()
    conn.execute(schema)
