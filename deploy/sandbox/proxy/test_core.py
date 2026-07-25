"""Phase 3 gate (local): the proxy's auth gates + key injection are correct.

Runs the proxy against a fake upstream (injected httpx ASGI transport), so the
security-critical behavior is verified without Modal or a real vendor: the
capability token is stripped and the real key injected; a revoked token is 401;
a disallowed path is 404; admin needs its token.
"""

from __future__ import annotations

from typing import Any

from core import Binding, SessionRegistry, build_proxy_app
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx
import pytest

REAL_KEY = "sk-REAL-vendor-key"


def _fake_upstream(seen: dict[str, Any]) -> FastAPI:
    up = FastAPI()

    @up.api_route("/{path:path}", methods=["POST", "GET"])
    async def echo(path: str, request: Request) -> JSONResponse:
        seen["headers"] = {k.lower(): v for k, v in request.headers.items()}
        seen["path"] = path
        return JSONResponse({"upstream": "ok"})

    return up


def _proxy(registry: SessionRegistry, seen: dict[str, Any]) -> FastAPI:
    upstream_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=_fake_upstream(seen)))
    return build_proxy_app(
        registry=registry,
        upstream_base="http://upstream",
        key_resolver=lambda ref: REAL_KEY,
        admin_token="ADMIN",
        client=upstream_client,
    )


async def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy")


@pytest.mark.asyncio
async def test_key_injection_strips_capability_token() -> None:
    seen: dict[str, Any] = {}
    registry = SessionRegistry()
    app = _proxy(registry, seen)
    async with await _client(app) as c:
        # Admin registers a token for conversation c1.
        r = await c.post(
            "/admin/register",
            headers={"x-admin-token": "ADMIN"},
            json={"token": "CAP1", "conversation_id": "c1", "credential_ref": "gemini"},
        )
        assert r.status_code == 200

        # A model call with the capability token.
        r = await c.post(
            "/v1beta/models/gemini-3.6-flash:generateContent",
            headers={"authorization": "Bearer CAP1"},
            json={"contents": []},
        )
        assert r.status_code == 200 and r.json() == {"upstream": "ok"}
        # The real key was injected; the capability token never reached upstream.
        assert seen["headers"].get("x-goog-api-key") == REAL_KEY
        assert "CAP1" not in seen["headers"].get("authorization", "")


@pytest.mark.asyncio
async def test_revoked_token_is_401() -> None:
    seen: dict[str, Any] = {}
    registry = SessionRegistry()
    registry.register("CAP1", Binding(conversation_id="c1", credential_ref="gemini"))
    app = _proxy(registry, seen)
    async with await _client(app) as c:
        ok = await c.post("/v1/messages", headers={"authorization": "Bearer CAP1"}, json={})
        assert ok.status_code == 200
        registry.revoke_conversation("c1")
        denied = await c.post("/v1/messages", headers={"authorization": "Bearer CAP1"}, json={})
        assert denied.status_code == 401


@pytest.mark.asyncio
async def test_disallowed_path_is_404_and_admin_needs_token() -> None:
    seen: dict[str, Any] = {}
    registry = SessionRegistry()
    registry.register("CAP1", Binding(conversation_id="c1", credential_ref="gemini"))
    app = _proxy(registry, seen)
    async with await _client(app) as c:
        bad = await c.post("/v1/secret-admin/keys", headers={"authorization": "Bearer CAP1"}, json={})
        assert bad.status_code == 404
        forbidden = await c.post("/admin/register", headers={"x-admin-token": "WRONG"}, json={})
        assert forbidden.status_code == 403
