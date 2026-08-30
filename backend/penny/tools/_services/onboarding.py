"""Injection seam for the onboarding-resolve op (website-owned persistence).

Resolving an onboarding step writes ``app_onboarding_items`` — website/app state
the agent domain must not import (AGENTS.local.md). The website constructs its
persistence-backed resolver and passes it to ``build_agent``, which threads it
into the ``resolve_onboarding_item`` tool; the agent domain sees only this
``OnboardingResolver`` Protocol, never ``penny.api.persistence``.
"""

from __future__ import annotations

from typing import Protocol


class OnboardingResolver(Protocol):
    """Persist a user's accept/decline of an onboarding step.

    Returns ``{item_key, status}`` on success, or ``{error}`` for an unknown
    key/action (a model mistake surfaces as recoverable tool output).
    """

    def __call__(self, item_key: str, action: str) -> dict[str, str]: ...
