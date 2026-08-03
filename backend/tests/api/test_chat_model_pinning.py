"""POST /api/chat pins the model; GET /api/sessions reports the pin.

The route reads ``selectedChatModel`` only on the conversation-creating
request: the first choice is validated against the selection (a 400 before the
stream opens — after that, errors can only be SSE frames), pinned to the row,
and every later turn runs on the pin regardless of what a client sends. The
agent construction is stubbed; what these tests pin is the wire contract and
which (model, effort) reach the factory.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from fastapi.testclient import TestClient
import pytest

import penny.agent_factory
import penny.api.main as main
from penny.api.persistence.store import ConversationAccessError, ConversationStore
import penny.api.routes as routes
from penny.db import get_db


@pytest.fixture
def built_models(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Stub the agent out; record the (name, effort) each turn was built with."""
    built: list[dict[str, Any]] = []

    def fake_build_model(*, credential: Any = None, name: str | None = None) -> Any:
        return {"name": name}

    def fake_build_agent(*, model: Any, effort: Any = None, **kwargs: Any) -> Any:
        built.append({"name": model["name"], "effort": effort})
        return object()

    async def fake_stream(agent: Any, prompt: str, **kwargs: Any) -> AsyncIterator[str]:
        yield "data: [DONE]\n\n"

    async def no_onboarding(conversation_id: str, reminders: Any) -> None:
        return None

    monkeypatch.setattr(penny.agent_factory, "build_model", fake_build_model)
    monkeypatch.setattr(penny.agent_factory, "build_agent", fake_build_agent)
    monkeypatch.setattr(routes, "stream_and_persist", fake_stream)
    monkeypatch.setattr(routes, "_maybe_enqueue_onboarding", no_onboarding)
    return built


def _post_chat(
    client: TestClient, chat_id: str, text: str, *, model: str | None = None
) -> Any:
    body: dict[str, Any] = {
        "id": chat_id,
        "message": {
            "id": f"u-{text}",
            "role": "user",
            "parts": [{"type": "text", "text": text}],
        },
    }
    if model is not None:
        body["selectedChatModel"] = model
    return client.post("/api/chat", json=body)


def _pinned(chat_id: str) -> str | None:
    return ConversationStore().get_conversation(chat_id).model


def test_first_choice_wins_and_later_turns_cannot_change_it(
    isolated_db, built_models: list[dict[str, Any]]
) -> None:
    with TestClient(main.app) as client:
        # The creating request carries the choice.
        r1 = _post_chat(client, "c-pin", "hi", model="claude-opus-5:xhigh")
        # The normal later turn carries none.
        r2 = _post_chat(client, "c-pin", "more")
        # A stray later request asking for something else is ignored.
        r3 = _post_chat(client, "c-pin", "again", model="gpt-5.5:low")

    assert (r1.status_code, r2.status_code, r3.status_code) == (200, 200, 200)
    assert _pinned("c-pin") == "claude-opus-5:xhigh"
    # Every turn ran on the pin — model name and effort split from the key.
    assert built_models == [{"name": "claude-opus-5", "effort": "xhigh"}] * 3


def test_a_turn_without_a_model_pins_the_configured_default(
    isolated_db, built_models: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PENNY_AGENT_MODEL", raising=False)

    with TestClient(main.app) as client:
        r = _post_chat(client, "c-default", "hi")

    assert r.status_code == 200
    assert _pinned("c-default") == "gemini-3.6-flash"
    assert built_models == [{"name": "gemini-3.6-flash", "effort": None}]


def test_unknown_model_is_refused_before_the_stream(
    isolated_db, built_models: list[dict[str, Any]]
) -> None:
    """A crisp 400, not an error frame mid-stream — and nothing persisted, so
    a retry with a real model is not stuck with a pinned typo."""
    with TestClient(main.app) as client:
        r = _post_chat(client, "c-bogus", "hi", model="claude-opus-999:xhigh")

    assert r.status_code == 400
    assert "claude-opus-999" in r.json()["detail"]
    assert built_models == []
    with pytest.raises(ConversationAccessError):
        ConversationStore().get_conversation("c-bogus")


def test_the_configured_default_is_always_acceptable(
    isolated_db, built_models: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The picker seeds from /api/config's default, which may be the configured
    (unoffered) Gemini default — echoing it back must not 400."""
    monkeypatch.delenv("PENNY_AGENT_MODEL", raising=False)

    with TestClient(main.app) as client:
        r = _post_chat(client, "c-echo", "hi", model="gemini-3.6-flash")

    assert r.status_code == 200
    assert _pinned("c-echo") == "gemini-3.6-flash"


def test_session_reports_the_pin(
    isolated_db, built_models: list[dict[str, Any]]
) -> None:
    with TestClient(main.app) as client:
        _post_chat(client, "c-hydrate", "hi", model="claude-sonnet-5:low")
        r = client.get("/api/sessions/c-hydrate")

    assert r.status_code == 200
    assert r.json()["model"] == "claude-sonnet-5:low"


def test_unpinned_conversation_reports_the_historical_model(
    isolated_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NULL means "predates model selection", and every such conversation ran
    on Gemini 3.6 Flash — a fixed constant, deliberately NOT the live default,
    so changing the default never relabels history."""
    monkeypatch.setenv("PENNY_AGENT_MODEL", "moonshotai/kimi-k3")
    get_db().create_schema()
    ConversationStore().ensure_conversation("c-legacy")

    with TestClient(main.app) as client:
        r = client.get("/api/sessions/c-legacy")

    assert r.status_code == 200
    assert r.json()["model"] == "gemini-3.6-flash"
