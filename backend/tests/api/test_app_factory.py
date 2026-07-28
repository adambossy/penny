"""The create_app composition seam, exercised the way a hosting product will.

Until penny-web exists as a real consumer, this test is the second caller of
``create_app``'s injection points: a per-request auth dependency, a custom
TurnWiring, and an extra router must all be honored.
"""

from __future__ import annotations

import contextlib

from fastapi import APIRouter, HTTPException, Request
from fastapi.testclient import TestClient

from penny.api.app import AppConfig, TurnProvision, create_app


def _auth_dependency(request: Request) -> None:
    if request.headers.get("x-test-token") != "letmein":
        raise HTTPException(status_code=401, detail="nope")


class _RecordingWiring:
    def __init__(self) -> None:
        self.turns: list[str] = []

    @contextlib.asynccontextmanager
    async def turn(self, conversation_id: str):
        self.turns.append(conversation_id)
        yield TurnProvision()


def _make_client() -> tuple[TestClient, _RecordingWiring]:
    extra = APIRouter()

    @extra.get("/api/host-only")
    async def host_only() -> dict[str, bool]:
        return {"host": True}

    wiring = _RecordingWiring()
    app = create_app(
        AppConfig(
            auth_dependency=_auth_dependency,
            turn_wiring=wiring,
            extra_routers=[extra],
        )
    )
    return TestClient(app), wiring


def test_auth_dependency_gates_every_route(isolated_db):
    client, _ = _make_client()
    assert client.get("/api/health").status_code == 401
    assert client.get("/api/conversations").status_code == 401
    assert client.get("/api/host-only").status_code == 401


def test_authed_requests_pass_and_extra_router_mounts(isolated_db):
    client, _ = _make_client()
    headers = {"x-test-token": "letmein"}
    # The with-block runs the lifespan (bootstrap -> schema) like a real serve.
    with client:
        assert client.get("/api/health", headers=headers).json() == {"ok": True}
        assert client.get("/api/host-only", headers=headers).json() == {"host": True}
        assert client.get("/api/conversations", headers=headers).status_code == 200


def test_default_app_needs_no_auth(isolated_db):
    client = TestClient(create_app())
    assert client.get("/api/health").json() == {"ok": True}
