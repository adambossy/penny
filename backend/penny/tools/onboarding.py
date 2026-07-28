"""Agent tool: record a user's accept/decline of an onboarding nudge.

Thin ``@tool`` over an injected ``OnboardingResolver`` (the website/app-domain
persistence op). The tool does not touch the web store directly — the website
constructs the resolver and threads it in via ``build_agent`` → ``build_toolset``
→ :func:`make_resolve_onboarding_item`, so the agent domain never imports
``penny.api.persistence`` (decision D5). The tool never *performs* the underlying
action (linking a bank, editing taxonomy): those are other tools. This only marks
the nudge resolved so onboarding stops surfacing it.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agent_harness import tool

from penny.tools._services.onboarding import OnboardingResolver


def make_resolve_onboarding_item(resolver: OnboardingResolver | None):
    """Build the ``resolve_onboarding_item`` tool bound to ``resolver``.

    ``resolver`` is injected by the website. When it is ``None`` (a front door
    that does not wire onboarding, e.g. the CLI/cron) the tool still exists but
    invoking it fails loudly rather than silently no-op'ing.
    """

    @tool
    async def resolve_onboarding_item(item_key: str, action: str) -> dict[str, Any]:
        """Mark an onboarding step accepted or dismissed for the current user.

        Call when the user explicitly accepts or declines a setup nudge. ``item_key``
        is one of the onboarding steps (e.g. ``connect_plaid``); ``action`` is
        ``"accepted"`` or ``"dismissed"``. A dismissed step is never nudged again,
        but the user can always ask later and you perform the action directly.
        Returns ``{"item_key", "status"}`` (or ``{"error"}`` for a bad key/action).
        """
        if resolver is None:
            raise RuntimeError("onboarding resolver is not configured for this run")
        return await asyncio.to_thread(resolver, item_key, action)

    return resolve_onboarding_item
