"""The provider follows from the model name (``penny.agent_factory``).

``PENNY_AGENT_MODEL`` selects both the model and how Penny reaches it, so
there is no second provider variable to drift out of sync. Two things are
easy to get wrong and expensive to notice — both fail at *request* time, not
construction time — so pin them here:

- Kimi K3 has a single upstream endpoint, so it needs ``MOONSHOT_DIRECT``;
  the harness's default policy matches zero endpoints and 404s.
- ``thinking_budget`` is provider-generic but its domain is not: ``-1`` means
  "dynamic" to Gemini and is rejected as ``budget_tokens`` on the
  Anthropic-shaped OpenRouter path.
"""

from __future__ import annotations

from agent_harness.providers.google import GeminiModel
from agent_harness.providers.openrouter import (
    GLM_5_2,
    KIMI_K3,
    US_FP8_ZDR,
    OpenRouterModel,
)
import pytest

from penny.agent_factory import _thinking_budget_from_env, build_model


@pytest.fixture(autouse=True)
def _clear_model_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PENNY_AGENT_MODEL", raising=False)
    monkeypatch.delenv("PENNY_AGENT_THINKING_BUDGET", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "test-google-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")


def test_gemini_name_builds_a_google_model() -> None:
    model = build_model(name="gemini-3.6-flash")
    assert isinstance(model, GeminiModel)
    assert model.name == "gemini-3.6-flash"


def test_openrouter_name_builds_an_openrouter_model() -> None:
    model = build_model(name=KIMI_K3)
    assert isinstance(model, OpenRouterModel)
    assert model.name == KIMI_K3
    # The credential guard rejects a non-OpenRouter key, so the provider tag
    # matters as much as the base URL.
    assert model.provider.name == "openrouter"


def test_kimi_k3_pins_moonshot_direct_routing() -> None:
    """Without this the default policy matches no endpoint and every call 404s."""
    model = build_model(name=KIMI_K3)
    assert model.routing.only == ("moonshotai",)


def test_glm_keeps_the_harness_default_routing() -> None:
    """Only K3 needs the opt-in; GLM stays on the vetted US/FP8/ZDR set."""
    model = build_model(name=GLM_5_2)
    assert model.routing == US_FP8_ZDR


def test_env_selects_the_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PENNY_AGENT_MODEL", KIMI_K3)
    assert isinstance(build_model(), OpenRouterModel)


def test_missing_openrouter_key_names_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match=r"OPENROUTER_API_KEY.*kimi-k3"):
        build_model(name=KIMI_K3)


def test_thinking_budget_defaults_per_dialect() -> None:
    """-1 is Gemini's "dynamic"; the Anthropic shape rejects a negative budget."""
    assert _thinking_budget_from_env(build_model(name="gemini-3.6-flash")) == -1
    assert _thinking_budget_from_env(build_model(name=KIMI_K3)) > 0


def test_explicit_thinking_budget_wins_for_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PENNY_AGENT_THINKING_BUDGET", "4096")
    assert _thinking_budget_from_env(build_model(name="gemini-3.6-flash")) == 4096
    assert _thinking_budget_from_env(build_model(name=KIMI_K3)) == 4096
