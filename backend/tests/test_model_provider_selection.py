"""The provider follows from the model name (``penny.agent_factory``).

Dispatch resolves the provider family from the harness catalogue, so a typo
or an arbitrary client string raises instead of silently becoming a Gemini
call (the old membership-test fall-through). Each non-Gemini branch resolves
its own env credential and names both the missing variable and the model in
its error, since these fail at *request* time otherwise. The thinking budget
is family-derived and no longer env-overridable — with per-conversation
models, one global number would hard-fail whichever family it wasn't written
for.
"""

from __future__ import annotations

from agent_harness.core.credentials import ApiKeyCredential
from agent_harness.core.errors import ConfigError
from agent_harness.providers.anthropic import OPUS_5, AnthropicMessagesModel
from agent_harness.providers.google import GeminiModel
from agent_harness.providers.openai import GPT_5_6_SOL, OpenAIResponsesModel
from agent_harness.providers.openrouter import (
    GLM_5_3,
    GLM_5_3_FLASH,
    KIMI_K3,
    US_FP8_ZDR,
    OpenRouterModel,
)
import pytest

from penny.agent_factory import _thinking_budget_for, build_model


@pytest.fixture(autouse=True)
def _clear_model_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PENNY_AGENT_MODEL", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "test-google-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-test")


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


def test_anthropic_name_builds_an_anthropic_model() -> None:
    model = build_model(name=OPUS_5)
    assert isinstance(model, AnthropicMessagesModel)
    assert model.name == OPUS_5
    assert model.provider.name == "anthropic"


def test_openai_name_builds_an_openai_model() -> None:
    model = build_model(name=GPT_5_6_SOL)
    assert isinstance(model, OpenAIResponsesModel)
    assert model.name == GPT_5_6_SOL
    assert model.provider.name == "openai"


def test_composite_selection_key_builds_the_bare_model() -> None:
    """The effort half of a pinned key belongs to ModelSettings, not the model."""
    model = build_model(name=f"{OPUS_5}:xhigh")
    assert isinstance(model, AnthropicMessagesModel)
    assert model.name == OPUS_5


def test_unknown_model_raises_instead_of_falling_through() -> None:
    """The old membership test sent every unknown id to Gemini, silently."""
    with pytest.raises(ValueError, match="not-a-model"):
        build_model(name="not-a-model")


def test_explicit_credential_must_match_the_provider_family() -> None:
    """With per-conversation models the family varies per request while a
    provisioned credential does not — a mismatch fails at construction naming
    both providers, not as an opaque vendor 401 mid-stream.

    The guard is the harness's, deliberately not duplicated in Penny: a local
    check would only preempt the library's clearer error with our own.
    """
    from agent_harness.core.credentials import ApiKeyCredential

    google_key = ApiKeyCredential(provider="google", key="test-key")
    with pytest.raises(ConfigError, match=r"'google'.*'anthropic'"):
        build_model(name=OPUS_5, credential=google_key)
    # A matching credential passes straight through.
    anthropic_key = ApiKeyCredential(provider="anthropic", key="test-key")
    model = build_model(name=OPUS_5, credential=anthropic_key)
    assert isinstance(model, AnthropicMessagesModel)


def test_kimi_k3_pins_moonshot_direct_routing() -> None:
    """Without this the default policy matches no endpoint and every call 404s."""
    model = build_model(name=KIMI_K3)
    assert model.routing.only == ("moonshotai",)


def test_glm_widens_the_vetted_us_fp8_zdr_set() -> None:
    """Only 3 of the harness default's 6 providers serve glm-5.3-flash live,
    so one upstream rate limit could abort a whole report run; Penny widens
    the same vetted policy with more US/FP8/ZDR providers so OpenRouter has
    somewhere to fall back to."""
    model = build_model(name=GLM_5_3)
    assert set(model.routing.only) > set(US_FP8_ZDR.only)
    assert "modal" in model.routing.only
    # The hard filters and soft knobs carry over from the harness default:
    # widening `only` must never loosen quantization/ZDR vetting, and
    # price-sort + fallbacks is what makes the wider set free resilience.
    assert model.routing.quantizations == US_FP8_ZDR.quantizations
    assert model.routing.zdr is True
    assert model.routing.sort == "price"
    assert model.routing.allow_fallbacks is True


def test_glm_flash_routes_the_same_as_glm() -> None:
    """One policy for the GLM family — flash must not drift from its sibling."""
    assert build_model(name=GLM_5_3_FLASH).routing == build_model(name=GLM_5_3).routing


def test_openrouter_models_widen_the_sdk_retry_budget() -> None:
    """OpenRouter surfaces upstream rate limits as plain 429s; the anthropic
    SDK's default budget of 2 backoff retries is shorter than the observed
    rate-limit windows, so a scheduled report died on its first call. The
    widened budget is defense-in-depth behind the widened provider set."""
    model = build_model(name=GLM_5_3_FLASH)
    assert model.provider.client.max_retries > 2


def test_env_selects_the_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PENNY_AGENT_MODEL", KIMI_K3)
    assert isinstance(build_model(), OpenRouterModel)


@pytest.mark.parametrize(
    ("env_var", "model_name", "match"),
    [
        ("OPENROUTER_API_KEY", KIMI_K3, r"OPENROUTER_API_KEY.*kimi-k3"),
        ("ANTHROPIC_API_KEY", OPUS_5, r"ANTHROPIC_API_KEY.*claude-opus-5"),
        ("OPENAI_API_KEY", GPT_5_6_SOL, r"OPENAI_API_KEY.*gpt-5.6-sol"),
    ],
)
def test_missing_provider_key_names_the_model(
    monkeypatch: pytest.MonkeyPatch, env_var: str, model_name: str, match: str
) -> None:
    monkeypatch.delenv(env_var, raising=False)
    with pytest.raises(RuntimeError, match=match):
        build_model(name=model_name)


def test_thinking_budget_is_family_derived() -> None:
    """-1 is Gemini's "dynamic"; every other family needs a positive gate value
    (the Anthropic wire shape rejects a negative budget; adaptive models and
    OpenAI need it non-None for the thinking block and its streamed summaries).
    """
    assert _thinking_budget_for(build_model(name="gemini-3.6-flash")) == -1
    assert _thinking_budget_for(build_model(name=KIMI_K3)) > 0
    assert _thinking_budget_for(build_model(name=OPUS_5)) > 0
    assert _thinking_budget_for(build_model(name=GPT_5_6_SOL)) > 0


def test_mismatched_explicit_credential_fails_at_construction() -> None:
    """A gate credential minted for another provider is rejected loudly at
    client-build time inside ``build_model`` (the harness's credential guard),
    never as a silent per-request failure against the wrong backend."""
    wrong_provider = ApiKeyCredential(provider="google", key="not-an-openrouter-key")
    with pytest.raises(ConfigError, match="openrouter"):
        build_model(name=KIMI_K3, credential=wrong_provider)
