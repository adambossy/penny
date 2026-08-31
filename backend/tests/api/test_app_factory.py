"""The create_app composition seam, exercised the way a hosting product will.

Until penny-web exists as a real consumer, this test is the second caller of
``create_app``'s injection points: a per-request auth dependency, a custom
TurnWiring, and an extra router must all be honored.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.testclient import TestClient

from penny.api.app import AppConfig, TurnProvision, create_app, default_static_dir


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


def test_default_instance_mounts_the_ui_that_default_static_dir_resolves():
    """The actual regression: `penny.api.main` was built as a bare
    `create_app()`, so the documented dev-loop command served /api but 404'd
    `/` and every SPA route while `penny serve` worked — one checkout, two
    answers (P15).

    `create_app`'s own static handling was never broken, so exercising that
    would pass against the bug; what has to be pinned is main.py's *wiring*.
    Phrased as an equality with `default_static_dir()` so it holds either way:
    an unbuilt checkout legitimately mounts nothing.
    """
    import penny.api.main as main

    expected = default_static_dir()
    mounts = [r for r in main.app.routes if getattr(r, "name", None) == "frontend"]

    if expected is None:
        assert not mounts, "nothing built, so nothing should be mounted"
    else:
        assert mounts, "the default instance must mount the built UI"
        assert Path(mounts[0].app.directory) == expected


def test_static_dir_serves_the_spa_without_shadowing_the_api(isolated_db, tmp_path):
    """A mounted UI answers `/` and every client-side route, and only those.

    The half that is easy to lose: the SPA fallback must not swallow unknown
    /api paths into index.html, which would turn a typo'd endpoint into a 200
    page for any client.
    """
    (tmp_path / "index.html").write_text("<!doctype html><title>Penny</title>")
    client = TestClient(create_app(AppConfig(static_dir=tmp_path)))

    with client:
        assert client.get("/").status_code == 200
        assert "Penny" in client.get("/c/some-conversation-id").text
        assert client.get("/api/health").json() == {"ok": True}
        unknown_api = client.get("/api/no-such-route")
        assert unknown_api.status_code == 404
        assert unknown_api.json() == {"detail": "Not Found"}


def test_no_static_dir_leaves_the_api_alone(isolated_db):
    """Nothing built yet is the API-only case, not an error (the Vite dev
    server proxies to it) — so `/` 404s rather than the app refusing to boot."""
    client = TestClient(create_app(AppConfig(static_dir=None)))

    with client:
        assert client.get("/api/health").json() == {"ok": True}
        assert client.get("/").status_code == 404
