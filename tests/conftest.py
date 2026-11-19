"""Test setup: every test gets a fresh copy of a seeded database, like a Labs Check.

Set MONEYSAFE_ADMIN_DSN to a Postgres role that may create databases, e.g.
    MONEYSAFE_ADMIN_DSN="postgresql://postgres@localhost/postgres"
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from moneysafe.db import connect, init_db
from moneysafe.ledger import Posting, open_shop, post_entry, shop_wallet

ADMIN_DSN = os.environ.get("MONEYSAFE_ADMIN_DSN", "postgresql:///postgres")
TEMPLATE = "moneysafe_test_template"


def scalar(conn, sql: str, params=None):
    """First column of the first row: scalar(conn, "SELECT COUNT(*) FROM payouts")."""
    return next(iter(conn.execute(sql, params).fetchone().values()))


def _admin():
    return psycopg.connect(ADMIN_DSN, autocommit=True)


@pytest.fixture(scope="session")
def template_db():
    with _admin() as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TEMPLATE} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {TEMPLATE}")
    with connect(make_conninfo(ADMIN_DSN, dbname=TEMPLATE)) as conn:
        init_db(conn)
    yield TEMPLATE
    with _admin() as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TEMPLATE} WITH (FORCE)")


@pytest.fixture
def dsn(template_db) -> str:
    name = f"moneysafe_test_{uuid.uuid4().hex[:12]}"
    with _admin() as admin:
        admin.execute(f"CREATE DATABASE {name} TEMPLATE {template_db}")
    yield make_conninfo(ADMIN_DSN, dbname=name)
    with _admin() as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@pytest.fixture
def conn(dsn):
    with connect(dsn) as c:
        yield c


@pytest.fixture
def fund_shop():
    """Give a shop wallet money as if customers had paid it."""
    def _fund(conn, shop_id: int, paise: int) -> None:
        open_shop(conn, shop_id)
        post_entry(conn, f"test funding for shop {shop_id}", [
            Posting("provider:clearing", paise),
            Posting(shop_wallet(shop_id), -paise),
        ])
    return _fund
