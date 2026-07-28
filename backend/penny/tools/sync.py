"""Plaid sync — fetch transactions across all connected items, categorize, persist.

Thin ``@tool`` wrapper over ``SyncTool`` (which itself orchestrates
``PlaidClient`` -> ``Categorizer`` -> ``PersistTool``). Cursor persistence
and dedup live inside the service layer.
"""

from __future__ import annotations

from typing import Any

from agent_harness import tool

from penny.adapters.clients.plaid import PlaidClient
from penny.db import get_db
from penny.services import build_categorizer, get_taxonomy
from penny.tools._services.sync_service import SyncTool


@tool
async def sync_transactions(count: int = 250) -> dict[str, Any]:
    """Sync the latest transactions from every connected Plaid item.

    Categorizes new and modified transactions and persists them. Cursor
    state is tracked per item so subsequent calls are incremental.

    Args:
        count: Max transactions per page. Default 250.

    Returns:
        ``{"status": "success", "items_synced", "total_added",
        "total_modified", "total_removed", ...}`` on success, or
        ``{"status": "error", "message": ...}`` on failure.
    """
    try:
        plaid_client = PlaidClient.from_env()
        sync_service = SyncTool(
            plaid_client=plaid_client,
            categorizer_factory=build_categorizer,
            db=get_db(),
            taxonomy=get_taxonomy(),
        )
        summary = await sync_service.sync(count=count)
        return {"status": "success", **summary.to_dict()}
    except Exception as exc:
        return {
            "status": "error",
            "message": str(exc),
            "items_synced": 0,
            "total_added": 0,
            "total_modified": 0,
            "total_removed": 0,
        }


@tool
async def sync_status() -> dict[str, Any]:
    """Report how fresh the local data is and whether the daemon is running.

    Reads the daemon's state file (last sync/report runs + outcomes) and the
    newest transaction timestamp. Use before analysis when data freshness
    matters; if the daemon is not running, suggest the user start it with
    ``penny daemon start``.
    """
    import asyncio
    from datetime import UTC, datetime

    def _run() -> dict[str, Any]:
        from sqlalchemy import func, select

        from penny.adapters.db.models import PlaidTransaction
        from penny.daemon import read_state

        state = read_state()
        with get_db().session() as s:
            newest = s.execute(
                select(func.max(PlaidTransaction.created_at))
            ).scalar_one_or_none()
        sync_state = state.get("sync") or {}
        return {
            "status": "success",
            "last_sync_at": sync_state.get("last_run_at"),
            "last_sync_ok": sync_state.get("ok"),
            "last_report": state.get("report"),
            "newest_transaction_at": newest.isoformat() if newest else None,
            "now": datetime.now(UTC).isoformat(),
            "daemon_state_present": bool(state),
        }

    return await asyncio.to_thread(_run)
