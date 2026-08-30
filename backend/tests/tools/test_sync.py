"""``sync_status`` surfaces the daemon's per-job state, including report jobs.

Regression: report jobs are namespaced ``report:<name>`` in daemon state
(one entry per configured job), but ``sync_status`` read a bare ``"report"``
key that no longer exists, so its report field was silently always ``None``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from penny.tools.sync import sync_status


@pytest.mark.asyncio
async def test_sync_status_surfaces_report_job_states() -> None:
    state = {
        "sync": {"last_run_at": "2026-08-03T13:00:00+00:00", "ok": True},
        "report:weekly": {"ok": True, "last_resolved_period": "2026-W31"},
        "report:monthly": {"ok": False, "last_resolved_period": "2026-07"},
    }
    db = MagicMock()
    db.max_plaid_transaction_created_at.return_value = None

    with (
        patch("penny.daemon_state.read_state", return_value=state),
        patch("penny.tools.sync.get_db", return_value=db),
    ):
        result = await sync_status.fn()

    assert result["last_sync_at"] == "2026-08-03T13:00:00+00:00"
    assert result["last_sync_ok"] is True
    assert result["report_jobs"] == {
        "weekly": {"ok": True, "last_resolved_period": "2026-W31"},
        "monthly": {"ok": False, "last_resolved_period": "2026-07"},
    }


@pytest.mark.asyncio
async def test_sync_status_empty_state() -> None:
    db = MagicMock()
    db.max_plaid_transaction_created_at.return_value = None

    with (
        patch("penny.daemon_state.read_state", return_value={}),
        patch("penny.tools.sync.get_db", return_value=db),
    ):
        result = await sync_status.fn()

    assert result["report_jobs"] == {}
    assert result["daemon_state_present"] is False
