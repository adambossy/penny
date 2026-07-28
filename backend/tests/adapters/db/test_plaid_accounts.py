from pathlib import Path

from penny.adapters.db.facade import DB
from penny.adapters.db.models import PlaidAccount, PlaidItem


def _create_db(tmp_path: Path) -> DB:
    db = DB(f"sqlite:///{tmp_path / 'test.db'}")
    db.create_schema()
    return db


def test_plaid_account_links_item(tmp_path):
    db = _create_db(tmp_path)
    with db.session() as session:
        item = PlaidItem(item_id="item-1", access_token="tok")
        session.add(item)
        session.flush()
        acct = PlaidAccount(
            account_id="acct-1",
            item_id="item-1",
            name="Checking",
        )
        session.add(acct)
        session.flush()
        assert acct.item_id == "item-1"
