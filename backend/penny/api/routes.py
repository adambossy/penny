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

from agent_harness.extras.reminders import ReminderQueue
from agent_harness.sessions.inmemory import InMemorySession
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from loguru import logger

from penny.config import agent_model
from penny.model_selection import (
    PRE_SELECTION_MODEL,
    is_acceptable_key,
    label_for,
    offered_choices,
    parse_key,
)

from .app import TurnWiring
from .bridge import _sse, stream_and_persist
from .hydration import conversation_to_ui
from .persistence.rehydrate import parts_to_messages
from .persistence.store import ConversationAccessError, ConversationStore

# The one home of the API namespace: routes mount under it, and the SPA
# fallback in app.py uses it to keep API 404s JSON.
API_PREFIX = "/api"

_SSE_HEADERS = {
    "x-vercel-ai-ui-message-stream": "v1",
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
}


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


async def _maybe_enqueue_onboarding(
    conversation_id: str, reminders: ReminderQueue | None
) -> None:
    """Enqueue the consolidated onboarding reminder for this turn.

    Called before ``agent.run`` so the harness flush picks the reminder up this
    same turn. No-op when the host provisions no reminder queue.
    """
    from penny.api.persistence.onboarding import evaluate_turn

    if reminders is None:
        return
    content = await asyncio.to_thread(evaluate_turn, conversation_id)
    if content:
        await reminders.enqueue(conversation_id, "onboarding", content)


def build_router(*, turn_wiring: TurnWiring) -> APIRouter:
    """The chat API router, with turns provisioned by ``turn_wiring``."""
    router = APIRouter(prefix=API_PREFIX)
    store = ConversationStore()

    @router.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @router.get("/config")
    def get_config() -> dict[str, Any]:
        """Runtime facts the UI displays — the model choices and the default.

        The UI holds no model knowledge of its own: the offered choices come
        from the model selection and the labels ride along, rather than being
        restated in the frontend where they would silently go stale.

        ``defaultModel`` is the model pinned to the most recently updated
        conversation — the last choice sticks for the next conversation —
        falling back to the configured default when nothing is pinned yet
        or when the pin is no longer acceptable. The picker seeds itself
        from this field, so the carry-forward costs no extra request.
        ``model`` (the configured default) predates the picker and keeps
        its shape.
        """
        model_id = agent_model()
        # Everything served as defaultModel must pass the chat route's own
        # validation: a pin can outlive its acceptability (the configured
        # default moved, or its model was delisted), and serving it anyway
        # would seed the picker with a key the creating request 400s on.
        latest = store.latest_model()
        if latest is None or not is_acceptable_key(latest):
            latest = model_id
        return {
            "model": {"id": model_id, "label": label_for(model_id)},
            "models": [
                {"key": choice.key, "label": choice.label}
                for choice in offered_choices()
            ],
            "defaultModel": latest,
        }

    # Handlers doing synchronous DB work are plain ``def`` so FastAPI runs
    # them in its threadpool instead of stalling the SSE event loop.

    @router.get("/conversations")
    def list_conversations() -> dict[str, Any]:
        """List conversations (newest-first) for the history drawer."""
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

    @router.get("/sessions/{session_id}")
    def get_session(session_id: str) -> dict[str, Any]:
        """Hydrate a conversation from the app store (the faithful path).

        Reads the captured ``conversation_messages`` rows — not the lossy
        harness transcript — so the rehydrated transcript matches what was
        streamed. (The ``/api/sessions`` path is kept for frontend
        compatibility; it reads the conversation store.)
        """
        try:
            conversation = store.get_conversation(session_id)
            rows = store.get_conversation_messages(session_id)
        except ConversationAccessError:
            raise HTTPException(status_code=404, detail="not found") from None
        return {
            "sessionId": session_id,
            "messages": conversation_to_ui(rows),
            # The pinned model; unpinned conversations report the model they
            # actually ran on (see PRE_SELECTION_MODEL), not the live default.
            "model": conversation.model or PRE_SELECTION_MODEL,
        }

    @router.post("/plaid/exchange")
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

        try:
            await asyncio.to_thread(store.get_conversation, conversation_id)
        except ConversationAccessError:
            raise HTTPException(status_code=404, detail="not found") from None

        return await exchange_public_token(
            public_token=public_token,
            conversation_id=conversation_id,
            queue=DbReminderQueue(),
        )

    @router.post("/chat")
    async def chat(request: Request) -> StreamingResponse:
        from penny.agent_factory import build_agent, build_model

        body: dict[str, Any] = await request.json()
        chat_id = str(body.get("id") or "default")
        prompt = _extract_prompt(body)
        user_message_id = _extract_user_message_id(body)

        # The model choice arrives only on the conversation-creating request;
        # later turns carry none, which is the normal case, not an error.
        # Validate BEFORE opening the turn (and before StreamingResponse —
        # past that point errors can only be SSE frames): an unknown key is a
        # crisp 400, never a silent fall-through to some other model, and
        # never a pinned typo. What counts as acceptable (offered, or the
        # configured default) is the selection's rule, not this route's.
        selected = body.get("selectedChatModel")
        if selected is not None and not (
            isinstance(selected, str) and is_acceptable_key(selected)
        ):
            raise HTTPException(status_code=400, detail=f"unknown model: {selected!r}")

        # One transaction, off the event loop: ensure the conversation,
        # capture the PRIOR-turn context (excludes this turn — the loop
        # appends the prompt), persist the user turn, derive the title, and
        # pin the model (create only — the first choice wins).
        opening = await asyncio.to_thread(
            store.begin_turn,
            chat_id,
            ai_sdk_message_id=user_message_id,
            text=prompt,
            model=selected or agent_model(),
        )
        prior_messages = parts_to_messages(opening.prior_messages)
        # The effective model: the pin, or the configured default for
        # conversations that predate model selection.
        model_name, effort = parse_key(opening.model or agent_model())

        session = InMemorySession(session_id=chat_id)
        if prior_messages:
            await session.add_messages(prior_messages)

        async def _stream() -> AsyncIterator[str]:
            try:
                async with turn_wiring.turn(chat_id) as provision:
                    await _maybe_enqueue_onboarding(chat_id, provision.reminders)
                    from penny.api.persistence.onboarding import resolve

                    agent = build_agent(
                        model=build_model(
                            credential=provision.credential, name=model_name
                        ),
                        effort=effort,
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
