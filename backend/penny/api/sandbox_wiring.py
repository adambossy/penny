"""Wire the sandboxed chat path into the FastAPI app (behind the flag).

Two clean halves, decoupled:

* **Display (pull).** ``POST /api/chat`` and the ``GET .../stream`` resume both
  pull the runner's event log over the Modal tunnel (``relay_turn``), translate
  to AI-SDK frames, and stream to the browser. They persist *nothing* — a browser
  disconnect ends the stream but never the turn.
* **Persistence (push).** When the turn finishes, the runner POSTs its whole
  event log to ``POST /api/chat/{id}/finalize`` (``finalize_turn``). Fly decodes,
  accumulates, persists (ctx-scoped), traces to Langfuse, and returns the sandbox
  to idle. This happens regardless of any browser, so the turn is durable even if
  the user closed the window.

Auth for the finalize callback is the same per-turn **capability token** the
runner already holds for MCP (machine-to-machine, conversation-scoped) — never
the user's session token. The token resolves to the turn's ``Principal`` (and its
tenant ``RequestContext``) via the shared ``mcp_registry``.

Everything secret-shaped is read from env, never hardcoded:
    PENNY_SANDBOX_IMAGE   the published runner image id
    PENNY_SANDBOX_APP     Modal app name (default penny-sandbox)
    PENNY_PROXY_URL       the deployed secrets-proxy base URL
    PENNY_PROXY_ADMIN     the proxy admin token (shared Fly↔proxy secret)
    PENNY_MCP_PUBLIC_URL  public URL of Fly's MCP server; its base also hosts the
                          finalize callback (enables tools + persistence)

Billing note: the proxy currently injects the platform Gemini key. Threading a
BYO/subsidy ``credential`` through to the proxy (and usage metering back to the
ledger) for sandboxed turns is a follow-up; the pre-dispatch billing *gate*
(Blocked → connect prompt) already runs in ``main.chat`` before we get here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
import contextlib
import os
import secrets
import time
from typing import Any

from agent_harness.core.credentials import Credential
from agent_harness.core.events import InMemoryEventBus
import httpx
from loguru import logger
from protocol.events import decode_envelope

from penny import observability
from penny.agent_factory import _render_system_prompt
from penny.config import agent_model
from penny.sandboxes.provider import ModalSandboxProvider
from penny.sandboxes.reaper import SandboxBusy, dispatch_turn, on_turn_end
from penny.sandboxes.relay import relay_turn
from penny.sandboxes.store import InMemorySandboxStore
from penny.tenancy.context import RequestContext, set_request_context

from .accumulator import MessageAccumulator
from .bridge import _safe_persist, _sse
from .mcp_server import CapabilityRegistry, Principal
from .persistence.store import ConversationStore

_provider: ModalSandboxProvider | None = None
_sandbox_store = InMemorySandboxStore()
# MCP capability registry, shared with the mounted MCP server (tools path) and
# the finalize callback (persistence path).
mcp_registry = CapabilityRegistry()
# conversation_id -> (tunnel_url, run_id, household_id) for an in-flight turn:
# the runner to resume from, and who may resume it. Cleared when the turn's
# finalize callback lands.
_active: dict[str, tuple[str, str, str]] = {}


def get_provider() -> ModalSandboxProvider:
    global _provider
    if _provider is None:
        image = os.environ["PENNY_SANDBOX_IMAGE"]
        _provider = ModalSandboxProvider(
            os.environ.get("PENNY_SANDBOX_APP", "penny-sandbox"), image
        )
    return _provider


async def _register_proxy_session(conversation_id: str) -> tuple[str, str]:
    """Mint a capability token and register it with the proxy. Returns
    (proxy_url, token)."""
    proxy_url = os.environ["PENNY_PROXY_URL"].rstrip("/")
    token = secrets.token_urlsafe(32)
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(
            f"{proxy_url}/admin/register",
            headers={"x-admin-token": os.environ["PENNY_PROXY_ADMIN"]},
            json={
                "token": token,
                "conversation_id": conversation_id,
                "credential_ref": "gemini",
            },
        )
        r.raise_for_status()
    return proxy_url, token


def _public_base() -> str | None:
    """Fly's public base URL (the tunnel the runner reaches), derived from the
    MCP public URL — same host serves MCP tools and the finalize callback."""
    mcp_url = os.environ.get("PENNY_MCP_PUBLIC_URL")
    if not mcp_url:
        return None
    return mcp_url.rstrip("/").removesuffix("/mcp").rstrip("/")


def _build_payload(
    conversation_id: str,
    prompt: str,
    system_prompt: str,
    seed_messages: list[dict[str, Any]],
    proxy_url: str,
    proxy_token: str,
    ctx: RequestContext,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "conversation_id": conversation_id,
        "prompt": prompt,
        "system_prompt": system_prompt,
        "seed_messages": seed_messages,
        "model": {
            "provider": "google",
            "name": agent_model(),
            "base_url": proxy_url,
            "capability_token": proxy_token,
            "thinking_budget": -1,
        },
    }
    base = _public_base()
    if base:
        # One capability token per turn authorizes BOTH the MCP tool calls and
        # the finalize callback — conversation-scoped, carries the tenant ctx.
        token = mcp_registry.mint(
            Principal(
                conversation_id=conversation_id,
                household_id=str(ctx.household_id),
                user_id=str(ctx.user_id),
                ctx=ctx,
            )
        )
        payload["mcp"] = {"url": f"{base}/mcp", "token": token}
        payload["persist"] = {
            "url": f"{base}/api/chat/{conversation_id}/finalize",
            "token": token,
        }
    return payload


async def _dispatch(
    conversation_id: str,
    ctx: RequestContext,
    prompt: str,
    seed_messages: list[dict[str, Any]],
) -> tuple[str, str]:
    """Dispatch a sandbox and start the turn. Returns (tunnel_url, run_id).
    Raises :class:`SandboxBusy` if a turn is already active."""
    provider = get_provider()
    rec = _sandbox_store.get(conversation_id)
    system_prompt = _render_system_prompt(ctx)
    proxy_url, proxy_token = await _register_proxy_session(conversation_id)
    payload = _build_payload(
        conversation_id,
        prompt,
        system_prompt,
        seed_messages,
        proxy_url,
        proxy_token,
        ctx,
    )
    handle = await dispatch_turn(rec, provider, now=time.time())
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=15.0)) as c:
        start = await c.post(f"{handle.tunnel_url.rstrip('/')}/turns", json=payload)
        start.raise_for_status()
        run_id = start.json()["run_id"]
    _active[conversation_id] = (handle.tunnel_url, run_id, str(ctx.household_id))
    return handle.tunnel_url, run_id


async def sandboxed_stream_and_persist(
    *,
    store: ConversationStore,
    conversation_id: str,
    ctx: RequestContext,
    prompt: str,
    seed_messages: list[dict[str, Any]],
    credential: Credential | None = None,
) -> AsyncIterator[str]:
    """POST /api/chat body: start the turn and stream it to the browser.

    Display only — persistence + tracing happen in the runner's finalize
    callback, so a browser disconnect ends this stream but not the turn. (``store``
    is unused here; kept for signature symmetry with the in-process path.)
    """
    del credential, store
    try:
        tunnel_url, run_id = await _dispatch(
            conversation_id, ctx, prompt, seed_messages
        )
    except SandboxBusy:
        # A turn is already in flight (e.g. a double-submit) — attach to it.
        entry = _active.get(conversation_id)
        if entry is None:
            yield _sse({"type": "error", "errorText": "a turn is already active"})
            yield "data: [DONE]\n\n"
            return
        tunnel_url, run_id, _hh = entry
    except Exception as exc:  # noqa: BLE001 - dispatch/transport failure before the run
        logger.bind(conversation_id=conversation_id).warning(
            "sandbox dispatch failed: {}", exc
        )
        yield _sse({"type": "error", "errorText": str(exc)})
        yield "data: [DONE]\n\n"
        return

    async for frame in relay_turn(tunnel_url, run_id, from_seq=0):
        yield _sse(frame)
    yield "data: [DONE]\n\n"


def resume_stream(
    conversation_id: str, ctx: RequestContext
) -> AsyncIterator[str] | None:
    """Follow an in-flight turn's runner log for a reconnecting browser
    (replay-then-follow). ``None`` (→ HTTP 204) when there is nothing to resume —
    the turn already finalized (or never ran), and the client uses the persisted
    transcript it hydrated on load."""
    entry = _active.get(conversation_id)
    if entry is None:
        return None
    tunnel_url, run_id, household_id = entry
    if household_id != str(ctx.household_id):
        return None  # not this principal's conversation — no existence leak

    async def _gen() -> AsyncIterator[str]:
        async for frame in relay_turn(tunnel_url, run_id, from_seq=0):
            yield _sse(frame)
        yield "data: [DONE]\n\n"

    return _gen()


async def finalize_turn(
    store: ConversationStore,
    conversation_id: str,
    token: str | None,
    events: list[dict[str, Any]],
) -> bool:
    """Persist a finished turn delivered by the runner's callback.

    Authenticates with the per-turn capability token (resolved to the turn's
    ``Principal`` + tenant ctx), then accumulates the event log and upserts the
    assistant message — idempotent, so a retried delivery is safe. Also traces to
    Langfuse and returns the sandbox to idle. Returns ``False`` (→ 401) on a bad
    or mismatched token.
    """
    principal = mcp_registry.resolve(token)
    if principal is None or principal.conversation_id != conversation_id:
        return False
    ctx = principal.ctx
    if ctx is None:
        return False

    # Trace the whole turn to Langfuse from the delivered log (browser-independent).
    trace_bus = InMemoryEventBus()
    trace_task = observability.start_run_trace_task(
        trace_bus,
        source="chat",
        session_id=conversation_id,
        user_id=str(ctx.user_id),
        prompt="",
    )
    set_request_context(ctx)
    acc = MessageAccumulator()
    try:
        for env in events:
            _seq, event = decode_envelope(env)
            acc.consume(event)
            await trace_bus.publish(event)
        _safe_persist(store, conversation_id, ctx, acc, acc.status)
    finally:
        await trace_bus.close()
        if trace_task is not None:
            with contextlib.suppress(Exception):
                await trace_task
        await on_turn_end(_sandbox_store.get(conversation_id), now=time.time())
        _active.pop(conversation_id, None)
        if token is not None:
            mcp_registry.revoke(token)  # one-time: turn is over
        set_request_context(None)
    return True


async def cancel_active_run(conversation_id: str) -> bool:
    """Route a browser stop to the runner's cancel endpoint; the runner then
    closes the turn and delivers its (partial) log via the finalize callback."""
    entry = _active.get(conversation_id)
    if entry is None:
        return False
    tunnel_url, run_id, _hh = entry
    with contextlib.suppress(Exception):
        async with httpx.AsyncClient(timeout=30.0) as c:
            await c.post(f"{tunnel_url.rstrip('/')}/runs/{run_id}/cancel")
    return True
