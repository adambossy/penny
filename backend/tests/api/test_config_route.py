"""GET /api/config: the UI asks which models it may offer, and the default.

The composer used to hardcode its model label, which quietly lied the moment
``PENNY_AGENT_MODEL`` named anything else. These pin the contract the UI now
depends on: the *configured* model (its label degrading to the raw id rather
than guessing), the offered choices, and the carried-forward default — the
model pinned to the most recently updated conversation, so a choice made once
seeds the next conversation's picker.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

import penny.api.main as main


def test_config_reports_the_configured_model(
    isolated_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PENNY_AGENT_MODEL", "moonshotai/kimi-k3")

    with TestClient(main.app) as client:
        r = client.get("/api/config")

    assert r.status_code == 200
    assert r.json()["model"] == {"id": "moonshotai/kimi-k3", "label": "Kimi K3"}


def test_config_defaults_to_gemini(
    isolated_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PENNY_AGENT_MODEL", raising=False)

    with TestClient(main.app) as client:
        r = client.get("/api/config")

    assert r.json()["model"] == {"id": "gemini-3.6-flash", "label": "Gemini 3.6 Flash"}


def test_unknown_model_falls_back_to_its_id(
    isolated_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An accurate unfamiliar id beats a pretty wrong one."""
    monkeypatch.setenv("PENNY_AGENT_MODEL", "some-vendor/experimental-9")

    with TestClient(main.app) as client:
        r = client.get("/api/config")

    model = r.json()["model"]
    assert model["id"] == "some-vendor/experimental-9"
    assert model["label"] == "some-vendor/experimental-9"


def test_config_lists_the_offered_choices(isolated_db) -> None:
    """The picker's list is generated from the selection — the UI holds no
    model knowledge of its own."""
    with TestClient(main.app) as client:
        r = client.get("/api/config")

    models = r.json()["models"]
    assert len(models) == 29
    assert all(set(entry) == {"key", "label"} for entry in models)
    keys = {entry["key"] for entry in models}
    assert "claude-opus-5:xhigh" in keys
    assert "moonshotai/kimi-k3" in keys
    assert "claude-sonnet-4-6:xhigh" not in keys  # the vendor has no xhigh there


def test_default_model_falls_back_to_configured(
    isolated_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PENNY_AGENT_MODEL", raising=False)

    with TestClient(main.app) as client:
        r = client.get("/api/config")

    assert r.json()["defaultModel"] == "gemini-3.6-flash"


def test_default_model_carries_the_last_pinned_choice_forward(
    isolated_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last choice sticks: the most recent pin wins, and unpinned
    (pre-feature) conversations are skipped rather than read as a choice."""
    from datetime import datetime

    from penny.api.persistence.models import Conversation
    from penny.api.persistence.store import ConversationStore
    from penny.db import get_db

    monkeypatch.delenv("PENNY_AGENT_MODEL", raising=False)
    get_db().create_schema()
    store = ConversationStore()
    store.begin_turn("old", ai_sdk_message_id="u1", text="a", model="gpt-5.5:low")
    store.begin_turn(
        "recent", ai_sdk_message_id="u2", text="b", model="claude-opus-5:xhigh"
    )
    store.ensure_conversation("unpinned")
    with store.session() as session:
        session.get(Conversation, "old").updated_at = datetime(2026, 1, 1)
        session.get(Conversation, "recent").updated_at = datetime(2026, 2, 1)
        session.get(Conversation, "unpinned").updated_at = datetime(2026, 3, 1)

    with TestClient(main.app) as client:
        r = client.get("/api/config")

    assert r.json()["defaultModel"] == "claude-opus-5:xhigh"
