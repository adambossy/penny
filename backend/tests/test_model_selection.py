"""The model selection (``penny.model_selection``): a filter over the catalogue.

Penny's one list of offered models — the picker, the chat route's validation
allowlist, and construction all read it. Effort levels are derived from the
harness catalogue, never restated, so the sharpest thing to pin is that no
offered entry pairs a model with an effort level its catalogue entry rejects.
"""

from __future__ import annotations

from agent_harness.providers import (
    anthropic as _anthropic,
    openai as _openai,
    openrouter as _openrouter,
)
import pytest

from penny.model_selection import (
    label_for,
    offered_choices,
    parse_key,
    provider_family,
    resolve_choice,
)

_EFFORT_LEVELS_BY_MODEL = {
    **_anthropic.EFFORT_LEVELS_BY_MODEL,
    **_openai.EFFORT_LEVELS_BY_MODEL,
    **_openrouter.EFFORT_LEVELS_BY_MODEL,
}


def test_offers_the_full_flat_list() -> None:
    """2 OpenRouter + (3 GPT × 4) + (3 Claude × 4) + (Sonnet 4.6 × 3) = 29."""
    assert len(offered_choices()) == 29


def test_no_offered_effort_is_rejected_by_the_catalogue() -> None:
    """The selection can only offer levels the harness says the model accepts."""
    for choice in offered_choices():
        if choice.effort is not None:
            assert choice.effort in _EFFORT_LEVELS_BY_MODEL[choice.model], choice.key


def test_sonnet_4_6_has_no_xhigh_entry() -> None:
    """Derived from the catalogue (the vendor has no xhigh there), not remembered."""
    sonnet_keys = {c.key for c in offered_choices() if c.model == _anthropic.SONNET_4_6}
    assert sonnet_keys == {
        "claude-sonnet-4-6:high",
        "claude-sonnet-4-6:medium",
        "claude-sonnet-4-6:low",
    }


def test_openrouter_models_carry_no_effort() -> None:
    for model in (_openrouter.KIMI_K3, _openrouter.GLM_5_2):
        choice = resolve_choice(model)
        assert choice is not None
        assert choice.effort is None
        assert choice.key == model  # bare key — no effort suffix


def test_keys_are_unique_and_resolve_back() -> None:
    choices = offered_choices()
    assert len({c.key for c in choices}) == len(choices)
    for choice in choices:
        assert resolve_choice(choice.key) == choice


def test_resolve_choice_refuses_unknown_keys() -> None:
    assert resolve_choice("claude-opus-5") is None  # effort is part of identity
    assert resolve_choice("gemini-3.6-flash") is None  # never offered
    assert resolve_choice("claude-sonnet-4-6:xhigh") is None  # vendor rejects


def test_parse_key_splits_model_and_effort() -> None:
    assert parse_key("claude-opus-5:xhigh") == ("claude-opus-5", "xhigh")
    assert parse_key("moonshotai/kimi-k3") == ("moonshotai/kimi-k3", None)
    assert parse_key("gemini-3.6-flash") == ("gemini-3.6-flash", None)


def test_parse_key_rejects_a_malformed_effort() -> None:
    with pytest.raises(ValueError, match="hyper"):
        parse_key("claude-opus-5:hyper")


def test_provider_family_covers_every_offered_model() -> None:
    """Everything offered can actually be dispatched — the list never
    advertises a model that fails at request time."""
    for choice in offered_choices():
        assert provider_family(choice.model) == choice.family


def test_provider_family_raises_on_unknown_ids() -> None:
    with pytest.raises(ValueError, match="gpt-9000"):
        provider_family("gpt-9000")


def test_label_for_keeps_the_legacy_gemini_label() -> None:
    """The pre-selection default still needs its display name (config +
    unpinned history), even though it is not offered."""
    assert label_for("gemini-3.6-flash") == "Gemini 3.6 Flash"
    assert label_for("moonshotai/kimi-k3") == "Kimi K3"
    assert label_for("some-vendor/experimental-9") == "some-vendor/experimental-9"
