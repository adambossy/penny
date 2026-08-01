"""SyncTool exercises the real DB façade surface.

Regression guard: the sweep/merchant tests all fake the DB, so a façade
rename (e.g. the tenancy-era ``list_plaid_items_for_context`` →
``list_plaid_items``) can slip through the suite while breaking every real
sync. Running the no-items path against a real façade pins the method names
SyncTool calls.
"""

from __future__ import annotations

from penny.db import get_db
from penny.tools._services.sync_service import SyncTool


async def test_sync_with_no_items_runs_against_the_real_facade(isolated_db):
    db = get_db()
    db.create_schema()
    tool = SyncTool(
        plaid_client=object(),  # never reached with zero linked items
        categorizer_factory=lambda: None,
        db=db,
        taxonomy=None,
    )
    summary = await tool.sync()
    assert summary.items_synced == 0
    assert summary.total_added == 0
