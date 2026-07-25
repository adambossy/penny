"""The turn payload: everything the runner needs to run one turn.

The Fly server renders the system prompt, resolves the session seed, and mints
the capability tokens, then ships this to the sandbox runner's ``POST /turns``.
The runner holds no secrets of its own — every URL and token here is
conversation-scoped and revocable, and the model/DB credentials live behind the
``proxy_url`` and ``mcp_url`` endpoints, never in this payload.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ModelConfig(BaseModel):
    """Which model to run, and where to reach it (the secrets proxy)."""

    provider: str = "google"  # "google" | "anthropic" | "openai"
    name: str = "gemini-3.6-flash"
    base_url: str | None = None  # the proxy URL; None means direct (dev only)
    # The capability token the proxy accepts in place of a real API key.
    capability_token: str | None = None
    thinking_budget: int = -1


class ToolServer(BaseModel):
    """The Fly MCP endpoint and this conversation's capability token."""

    url: str
    token: str


class PersistCallback(BaseModel):
    """Where the runner POSTs its authoritative turn-result callback."""

    url: str
    token: str


class TurnPayload(BaseModel):
    """One turn's full instruction set. JSON body of ``POST /turns``."""

    conversation_id: str
    prompt: str
    system_prompt: str = ""
    # Prior turns as encoded harness ``Message`` payloads (see events codec
    # ``__pyd__`` shape); loaded into the runner's in-memory session.
    seed_messages: list[dict[str, Any]] = Field(default_factory=list)
    model: ModelConfig = Field(default_factory=ModelConfig)
    mcp: ToolServer | None = None
    persist: PersistCallback | None = None
