"""The chat API routes, parameterized by the host's :class:`TurnWiring`.

Route bodies are the single-player chat surface: conversations, hydration,
the Plaid exchange, and the streaming chat turn. ``build_router`` closes over
the turn wiring so a hosting product changes how turns are provisioned
without touching the routes.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from agent_harness.sessions.inmemory import InMemorySession
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from loguru import logger

from penny.db import get_db

from .bridge import _sse, stream_and_persist
from .hydration import conversation_to_ui
from .persistence.rehydrate import parts_to_messages
from .persistence.store import ConversationAccessError, ConversationStore

_SSE_HEADERS = {
    "x-vercel-ai-ui-message-stream": "v1",
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
}

_conversation_store: ConversationStore | None = None


def _get_conversation_store() -> ConversationStore:
    global _conversation_store
    if _conversation_store is None:
        _conversation_store = ConversationStore()
    return _conversation_store


def _text_from_message(message: dict[str, Any]) -> str:
    parts = message.get("parts") or []
    text = "".join(
        part.get("text", "")
        for part in parts
        if isinstance(part, dict) and part.get("type") == "text"
    )
    if text:
        return text
    content = message.get("content")
    return content if isinstance(content, str) else ""


def _extract_user_message_id(body: dict[str, Any]) -> str | None:
    """Read the AI SDK message id of the inbound user turn, if present."""
    message = body.get("message")
    if isinstance(message, dict):
        mid = message.get("id")
        return mid if isinstance(mid, str) else None
    messages = body.get("messages")
    if isinstance(messages, list):
        for entry in reversed(messages):
            if isinstance(entry, dict) and entry.get("role") == "user":
                mid = entry.get("id")
                return mid if isinstance(mid, str) else None
    return None


def _extract_prompt(body: dict[str, Any]) -> str:
    """Read the latest user text from the AI SDK chat POST body.

    The real transport sends a single ``message``; fall back to a ``messages``
    array for other triggers.
    """
    message = body.get("message")
    if isinstance(message, dict):
        return _text_from_message(message)
    messages = body.get("messages")
    if isinstance(messages, list):
        for entry in reversed(messages):
            if isinstance(entry, dict) and entry.get("role") == "user":
                text = _text_from_message(entry)
                if text:
                    return text
    return ""


def _build_turn_signals(conversation_id: str) -> Any:
    """Gather the deterministic onboarding signals for this turn."""
    from penny.adapters.db.models import PlaidItem
    from penny.api.persistence.onboarding import TurnSignals

    db = get_db()
    with db.session() as s:
        has_linked = s.query(PlaidItem).count() > 0
    return TurnSignals(
        has_linked_items=has_linked,
        response_had_categorized_rows=False,
        user_corrected_category=False,
        conversation_id=conversation_id,
    )


async def _maybe_enqueue_onboarding(conversation_id: str, reminders: Any) -> None:
    """Enqueue the consolidated onboarding reminder for this turn.

    Called before ``agent.run`` so the harness flush picks the reminder up this
    same turn. No-op when the host provisions no reminder queue.
    """
    from penny.api.persistence.onboarding import ensure_items, evaluate
    from penny.api.persistence.reminders import _web_session

    if reminders is None:
        return
    signals = await asyncio.to_thread(_build_turn_signals, conversation_id)

    def _evaluate() -> str | None:
        with _web_session() as s:
            ensure_items(s)
            return evaluate(s, signals)

    content = await asyncio.to_thread(_evaluate)
    if content:
        await reminders.enqueue(conversation_id, "onboarding", content)


def build_router(*, turn_wiring: Any) -> APIRouter:
    """The chat API router, with turns provisioned by ``turn_wiring``."""
    router = APIRouter()

    @router.get("/api/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @router.get("/api/conversations")
    async def list_conversations() -> dict[str, Any]:
        """List conversations (newest-first) for the history drawer."""
        store = _get_conversation_store()
        rows = store.list_conversations()
        return {
            "conversations": [
                {
                    "id": row.conversation_id,
                    "title": row.title,
                    "updated_at": row.updated_at.isoformat(),
                }
                for row in rows
            ]
        }

    @router.get("/api/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        """Hydrate a conversation from the app store (the faithful path).

        Reads the captured ``conversation_messages`` rows — not the lossy
        harness transcript — so the rehydrated transcript matches what was
        streamed. (The ``/api/sessions`` path is kept for frontend
        compatibility; it reads the conversation store.)
        """
        store = _get_conversation_store()
        try:
            rows = store.get_conversation_messages(session_id)
        except ConversationAccessError:
            raise HTTPException(status_code=404, detail="not found") from None
        return {"sessionId": session_id, "messages": conversation_to_ui(rows)}

    @router.post("/api/plaid/exchange")
    async def plaid_exchange(request: Request) -> dict[str, Any]:
        """Exchange a Plaid ``public_token`` server-side.

        Body: ``{public_token, conversation_id}``. Verifies the conversation
        exists (404 otherwise) before exchanging, persisting the linked
        item/accounts, and enqueueing the success reminder.
        """
        from penny.api.persistence.reminders import DbReminderQueue
        from penny.tools._services.plaid_link import exchange_public_token

        body: dict[str, Any] = await request.json()
        public_token = body.get("public_token")
        conversation_id = body.get("conversation_id")
        if not isinstance(public_token, str) or not isinstance(conversation_id, str):
            raise HTTPException(
                status_code=400, detail="public_token and conversation_id are required"
            )

        store = _get_conversation_store()
        try:
            store.get_conversation(conversation_id)
        except ConversationAccessError:
            raise HTTPException(status_code=404, detail="not found") from None

        db = get_db()
        with db.session() as s:
            return await exchange_public_token(
                s,
                public_token=public_token,
                conversation_id=conversation_id,
                queue=DbReminderQueue(),
            )

    @router.post("/api/chat")
    async def chat(request: Request) -> StreamingResponse:
        from penny.agent_factory import build_agent, build_model

        body: dict[str, Any] = await request.json()
        chat_id = str(body.get("id") or "default")
        prompt = _extract_prompt(body)
        user_message_id = _extract_user_message_id(body)

        store = _get_conversation_store()
        store.ensure_conversation(chat_id)

        # PRIOR-turn context, captured BEFORE the current user turn is
        # appended, so it excludes this turn (the loop appends the prompt).
        prior_messages = parts_to_messages(store.get_conversation_messages(chat_id))

        # Persist the user turn up front (durable on mid-stream disconnect).
        store.append_user_message(
            chat_id, ai_sdk_message_id=user_message_id, text=prompt
        )
        store.set_title_if_unset(chat_id, prompt)

        session = InMemorySession(session_id=chat_id)
        if prior_messages:
            await session.add_messages(prior_messages)

        async def _stream() -> AsyncIterator[str]:
            try:
                async with turn_wiring.turn(chat_id) as provision:
                    await _maybe_enqueue_onboarding(chat_id, provision.reminders)
                    from penny.api.persistence.onboarding import resolve

                    agent = build_agent(
                        model=build_model(credential=provision.credential),
                        session=session,
                        persist_session=False,
                        workspace_dir=provision.workspace_dir,
                        usage_pricer=provision.usage_pricer,
                        reminders=provision.reminders,
                        onboarding_resolver=resolve,
                    )
                    async for frame in stream_and_persist(
                        agent,
                        prompt,
                        store=store,
                        conversation_id=chat_id,
                        subscribe_bus=provision.subscribe_bus,
                    ):
                        yield frame
            except Exception as exc:
                logger.exception("pre-stream setup failed for conversation {}", chat_id)
                yield _sse({"type": "error", "errorText": str(exc)})
                yield "data: [DONE]\n\n"

        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )

    return router
