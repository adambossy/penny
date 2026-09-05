"""The category review surface: label transactions, then see the scoreboard.

Website-domain CRUD over finance data — it reaches down through the finance
facade and never touches the agent domain. The labels it records are ordinary
verified categorizations, so the categorizer's fast path picks them up: a
label both scores the model and improves it.

Routes:

- ``GET /review`` — the labeling page (server-rendered HTML, no build step).
- ``GET /review/metrics`` — accuracy over the labeled rows.
- ``POST /api/review/label`` — record one label.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .routes import API_PREFIX

# One screenful of work at a time: the queue is ordered newest-first, so a
# cap bounds the page's size without hiding anything a reviewer would reach
# in one sitting — the next load brings the next batch.
_QUEUE_LIMIT = 200


class LabelRequest(BaseModel):
    transaction_id: int
    category_key: str


def _categories() -> list[dict[str, str]]:
    """The taxonomy as combobox options (every node — leaves and parents)."""
    from penny.services import get_taxonomy

    return [{"key": node.key, "name": node.name} for node in get_taxonomy().all_nodes()]


def build_review_router() -> APIRouter:
    """The review page + its label endpoint."""
    router = APIRouter()

    @router.get("/review", response_class=HTMLResponse, include_in_schema=False)
    async def review_page(day: str | None = None, all: bool = False) -> HTMLResponse:
        """One sync day's batch by default; ``?day=`` picks one, ``?all=1`` opens
        the whole backlog for a deliberate catch-up session."""
        from penny.api.review_page import render_review_page
        from penny.db import get_db

        try:
            chosen = date.fromisoformat(day) if day else None
        except ValueError as exc:
            raise HTTPException(400, f"Not a date: {day}") from exc
        batch = get_db().review_batch(day=chosen, all_days=all, limit=_QUEUE_LIMIT)
        return HTMLResponse(render_review_page(batch, _categories()))

    @router.get("/review/metrics", response_class=HTMLResponse, include_in_schema=False)
    async def review_metrics() -> HTMLResponse:
        from penny.api.review_page import render_metrics_page
        from penny.db import get_db

        db = get_db()
        return HTMLResponse(
            render_metrics_page(db.review_scoreboard(), db.pending_review_count())
        )

    @router.post(f"{API_PREFIX}/review/label")
    async def label(req: LabelRequest) -> dict[str, Any]:
        from penny.db import get_db
        from penny.services import get_taxonomy
        from penny.taxonomy.loader import get_category_id

        db = get_db()
        taxonomy = get_taxonomy()
        if not taxonomy.is_valid_key(req.category_key):
            raise HTTPException(400, f"Unknown category: {req.category_key}")
        category_id = get_category_id(db, taxonomy, req.category_key)
        if category_id is None:
            raise HTTPException(
                400, f"Category not in the database: {req.category_key}"
            )
        try:
            return db.mark_transaction_reviewed(req.transaction_id, category_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    return router
