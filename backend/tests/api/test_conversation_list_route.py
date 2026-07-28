"""GET /api/conversations: the drawer's list shape."""

from __future__ import annotations

from fastapi.testclient import TestClient

import penny.api.main as main
from penny.api.persistence.store import ConversationStore


def test_conversation_list_shape(isolated_db):
    # input: two conversations in the app store
    store = ConversationStore()
    store.create_schema()
    store.ensure_conversation("c-1")
    store.ensure_conversation("c-2")

    # act
    with TestClient(main.app) as client:
        r = client.get("/api/conversations")

    # expected: each entry carries the fields the history drawer renders
    assert r.status_code == 200
    conversations = r.json()["conversations"]
    assert {c["id"] for c in conversations} == {"c-1", "c-2"}
    assert {"id", "title", "updated_at"} <= set(conversations[0])
