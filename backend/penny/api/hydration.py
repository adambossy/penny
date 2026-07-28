"""Render stored conversation messages as AI SDK ``UIMessage`` JSON.

Used by ``GET /api/sessions/{id}`` so the frontend can rehydrate a conversation
after a refresh or backend restart. Because the bridge captured every part at
full fidelity (``MessageAccumulator``), the stored ``parts`` array is already in
UI shape — hydration is near-passthrough. There is no longer any need to
reconstruct UI parts from the lossy harness transcript, so the old
``messages_to_ui`` path (and its ``_parse_maybe_json`` / ``_collect_tool_results``
heuristics and the ``ThinkingBlock`` special-case) are gone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from penny.api.persistence.models import ConversationMessage


def conversation_to_ui(
    rows: list[ConversationMessage],
) -> list[dict[str, Any]]:
    """Render stored conversation messages as AI SDK UIMessage dicts.

    Parts pass through verbatim (they were stored in UI shape). The message id
    is the AI SDK message id when present, else a stable ``hist_<seq>`` fallback.
    Messages with no parts (e.g. a bare streaming placeholder that never got
    content) are skipped so they don't render as empty bubbles.
    """
    ui_messages: list[dict[str, Any]] = []
    for row in rows:
        parts = row.parts or []
        if not parts:
            continue
        message_id = row.ai_sdk_message_id or f"hist_{row.seq}"
        message: dict[str, Any] = {"id": message_id, "role": row.role, "parts": parts}
        ui_messages.append(message)
    return ui_messages
