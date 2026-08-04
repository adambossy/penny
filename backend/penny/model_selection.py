"""The model selection — which harness models Penny offers, keyed for the wire.

The harness owns the model catalogue: which models exist, their capabilities,
and which effort levels each accepts. Penny never restates any of that. What
this module adds is only what is genuinely Penny's:

- **Which families to offer** — a product decision (``_OFFERED``), the reason
  this is a filter over the catalogue rather than a passthrough.
- **Display labels** — presentation, the one fact the harness has no opinion
  about.
- **A composite key** of model + effort (``claude-opus-5:xhigh``). A
  conversation pinned to Opus 5 at extra-high effort and one at low effort are
  different choices, so effort is part of the stored identity. The key is what
  crosses the wire, what gets validated, and what lands in the database.

Offered effort levels are derived from the harness's per-model tables, so a
level a vendor rejects cannot be offered: Sonnet 4.6 has no ``xhigh`` entry in
the catalogue, so no such key can exist here — removed by construction, not by
remembering.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

from agent_harness.core.models import Effort
from agent_harness.providers import (
    anthropic as _anthropic,
    openai as _openai,
    openrouter as _openrouter,
)

from .config import agent_model

ProviderFamily = Literal["anthropic", "openai", "openrouter", "google"]
"""The provider families ``build_model`` dispatches on."""


@dataclass(frozen=True, slots=True)
class ModelChoice:
    """One offered picker entry: a model, optionally at a fixed effort."""

    key: str
    """The composite wire/DB key: ``<model>:<effort>``, or the bare model id."""

    model: str
    """The vendor model id the harness constructs."""

    effort: Effort | None
    """The effort level baked into this choice; ``None`` sends no effort."""

    family: ProviderFamily
    label: str


# Picker order for effort variants: strongest first, matching the fan-out in
# the offered list below. ``max`` is deliberately not offered (product choice;
# the harness supports it where vendors do).
_EFFORT_ORDER: tuple[Effort, ...] = ("xhigh", "high", "medium", "low")

_EFFORT_LABELS: dict[Effort, str] = {
    "xhigh": "Extra High",
    "high": "High",
    "medium": "Medium",
    "low": "Low",
}

# The product decision: which models the picker offers, in display order.
# ``fan_out_effort`` entries become one choice per offered effort level the
# catalogue accepts; the others are offered bare (no effort on the wire).
_OFFERED: tuple[tuple[ProviderFamily, str, str, bool], ...] = (
    ("openrouter", _openrouter.KIMI_K3, "Kimi K3", False),
    ("openrouter", _openrouter.GLM_5_2, "GLM-5.2", False),
    ("openai", _openai.GPT_5_6_SOL, "GPT 5.6 Sol", True),
    ("openai", _openai.GPT_5_6_TERRA, "GPT 5.6 Terra", True),
    ("openai", _openai.GPT_5_5, "GPT 5.5", True),
    ("anthropic", _anthropic.OPUS_5, "Opus 5", True),
    ("anthropic", _anthropic.OPUS_4_8, "Opus 4.8", True),
    ("anthropic", _anthropic.SONNET_5, "Sonnet 5", True),
    ("anthropic", _anthropic.SONNET_4_6, "Sonnet 4.6", True),
)

PRE_SELECTION_MODEL = "gemini-3.6-flash"
"""What a conversation with no pinned model (NULL column) actually ran on.

A fixed historical constant, deliberately NOT the configured default: every
unpinned conversation predates model selection and ran on Gemini 3.6 Flash,
so reporting the live default instead would relabel history the moment the
default moves.
"""

# Models Penny no longer (or never) offers but must still label: the
# pre-selection default every unpinned conversation actually ran on.
_LEGACY_LABELS = {PRE_SELECTION_MODEL: "Gemini 3.6 Flash"}

# The per-family effort tables, straight from the harness catalogue.
_EFFORT_LEVELS_BY_FAMILY: dict[str, dict[str, tuple[Effort, ...]]] = {
    "anthropic": _anthropic.EFFORT_LEVELS_BY_MODEL,
    "openai": _openai.EFFORT_LEVELS_BY_MODEL,
    "openrouter": _openrouter.EFFORT_LEVELS_BY_MODEL,
}


def _compose_key(model: str, effort: Effort | None) -> str:
    return f"{model}:{effort}" if effort is not None else model


def _build_choices() -> tuple[ModelChoice, ...]:
    choices: list[ModelChoice] = []
    for family, model, label, fan_out_effort in _OFFERED:
        if not fan_out_effort:
            choices.append(
                ModelChoice(
                    key=model, model=model, effort=None, family=family, label=label
                )
            )
            continue
        supported = _EFFORT_LEVELS_BY_FAMILY[family][model]
        for effort in _EFFORT_ORDER:
            if effort not in supported:
                continue
            choices.append(
                ModelChoice(
                    key=_compose_key(model, effort),
                    model=model,
                    effort=effort,
                    family=family,
                    label=f"{label} ({_EFFORT_LABELS[effort]})",
                )
            )
    return tuple(choices)


_CHOICES: tuple[ModelChoice, ...] = _build_choices()
_CHOICES_BY_KEY: dict[str, ModelChoice] = {choice.key: choice for choice in _CHOICES}


def offered_choices() -> tuple[ModelChoice, ...]:
    """Every offered picker entry, in display order.

    Drives the picker, the chat route's validation allowlist, and (via the
    keys) what gets pinned to conversations — one source for all three.
    """
    return _CHOICES


def resolve_choice(key: str) -> ModelChoice | None:
    """The offered choice for a composite key, or ``None`` if not offered."""
    return _CHOICES_BY_KEY.get(key)


def is_acceptable_key(key: str) -> bool:
    """Whether a client may start a conversation on this key.

    Offered choices are acceptable, and so is the configured default even
    when unoffered: the picker seeds from ``defaultModel``, which on a fresh
    install is the configured (unoffered) model — echoing it back must not be
    refused. This is the chat route's whole validation rule, kept here so no
    other front door re-derives half of it.
    """
    return key in _CHOICES_BY_KEY or key == agent_model()


def parse_key(key: str) -> tuple[str, Effort | None]:
    """Split a composite key into ``(model, effort)``.

    Accepts bare model ids (effort ``None``) so config defaults and legacy
    pins parse the same way. Deliberately does NOT check the offered list: a
    pinned key must stay buildable even after its entry is delisted, so old
    conversations keep working. A malformed effort suffix raises — that is a
    corrupt pin or a config typo, and building some other model instead would
    hide it.
    """
    model, sep, effort = key.partition(":")
    if not sep:
        return key, None
    if effort not in get_args(Effort):
        raise ValueError(f"unknown effort level {effort!r} in model key {key!r}")
    return model, effort  # type: ignore[return-value]


def provider_family(model: str) -> ProviderFamily:
    """The provider family for a bare model id.

    Resolves from the harness catalogues, so an unknown id raises instead of
    silently falling through to some provider (the old membership-test dispatch
    sent every typo to Gemini). Gemini itself has no harness catalogue — the
    Google provider takes any model name — so the family is claimed by the
    ``gemini`` id prefix.
    """
    for family, table in _EFFORT_LEVELS_BY_FAMILY.items():
        if model in table:
            return family  # type: ignore[return-value]
    if model.startswith("gemini"):
        return "google"
    raise ValueError(
        f"unknown model {model!r}: not in the harness catalogue and not a Gemini id"
    )


def label_for(key: str) -> str:
    """Display label for a key; unknown keys report their raw id.

    An unfamiliar-but-accurate label beats a pretty stale one — that is the
    bug the config endpoint exists to kill.
    """
    choice = _CHOICES_BY_KEY.get(key)
    if choice is not None:
        return choice.label
    return _LEGACY_LABELS.get(key, key)
