"""The model-name resolution contract (``penny.config``).

Every module that needs a model name resolves it through these accessors —
guard the env-var chain so a hardcoded model string can't silently reappear.
"""

from __future__ import annotations

import pytest

from penny.config import DEFAULT_AGENT_MODEL, agent_model, categorizer_model


@pytest.fixture(autouse=True)
def _clear_model_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PENNY_AGENT_MODEL", raising=False)
    monkeypatch.delenv("PENNY_CATEGORIZER_MODEL", raising=False)


def test_agent_model_defaults() -> None:
    assert agent_model() == DEFAULT_AGENT_MODEL


def test_agent_model_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PENNY_AGENT_MODEL", "gemini-9-flash")
    assert agent_model() == "gemini-9-flash"


def test_categorizer_model_falls_back_to_agent_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert categorizer_model() == DEFAULT_AGENT_MODEL
    monkeypatch.setenv("PENNY_AGENT_MODEL", "gemini-9-flash")
    assert categorizer_model() == "gemini-9-flash"


def test_categorizer_model_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PENNY_AGENT_MODEL", "gemini-9-flash")
    monkeypatch.setenv("PENNY_CATEGORIZER_MODEL", "gemini-9-categorizer")
    assert categorizer_model() == "gemini-9-categorizer"
