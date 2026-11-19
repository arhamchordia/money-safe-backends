from __future__ import annotations

from contextlib import closing

import pytest

from moneysafe.db import connect, init_db
from moneysafe.ledger import Posting, open_shop, post_entry, shop_wallet


@pytest.fixture
def db_path(tmp_path) -> str:
    path = str(tmp_path / "moneysafe.db")
    with closing(connect(path)) as conn:
        init_db(conn)
    return path


@pytest.fixture
def conn(db_path):
    with closing(connect(db_path)) as c:
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
