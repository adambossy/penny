"""Categorizer ``ModelSettings`` follow the model (``categorizer_agent``).

``categorizer_model()`` falls back to ``agent_model()``, so setting only
``PENNY_AGENT_MODEL`` re-points the categorizer too. Its settings carry the
same two hazards the chat agent pins: a ``-1`` thinking budget is Gemini's
"dynamic" but is rejected as ``budget_tokens`` on the Anthropic-shaped
OpenRouter path, and Google's grounding builtin is unknown to any non-Gemini
model — both fail at request time, so pin them here.
"""

from __future__ import annotations

from agent_harness.providers.openrouter import KIMI_K3
import pytest

from penny.agent_factory import build_model
from penny.tools._services.categorizer_agent import (
    _GEMINI_WEB_SEARCH_TOOL,
    _categorizer_model_settings,
)


@pytest.fixture(autouse=True)
def _model_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PENNY_AGENT_THINKING_BUDGET", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "test-google-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")


def test_gemini_settings_keep_dynamic_thinking_and_grounding() -> None:
    settings = _categorizer_model_settings(build_model(name="gemini-3.6-flash"))
    assert settings.thinking_budget == -1
    assert settings.builtin_tools == [_GEMINI_WEB_SEARCH_TOOL]


def test_openrouter_settings_drop_the_gemini_assumptions() -> None:
    """A ``-1`` budget 400s as ``budget_tokens``; ``google_search`` is not a
    tool the Anthropic-shaped path knows."""
    settings = _categorizer_model_settings(build_model(name=KIMI_K3))
    assert settings.thinking_budget > 0
    assert settings.builtin_tools == []
