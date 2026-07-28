"""The FastAPI app factory — the composition seam for hosts.

``create_app`` builds the chat API. The single-player default (``penny
serve``) needs no arguments; a hosting product (penny-web) composes the same
app with its own per-request authentication, per-turn wiring (credentialing,
workspace, reminders), and extra routers — it never forks the chat route.

Seams:

- ``auth_dependency`` — a FastAPI dependency run on every ``/api`` route.
  ``None`` (default) means no auth: every request is the one local user.
- ``turn_wiring`` — how a chat turn is provisioned: the model credential,
  the reminder queue, and an optional workspace bracket around the streamed
  run. ``LocalTurnWiring`` is the single-player default.
- ``extra_routers`` — additional APIRouters the host mounts (billing,
  account management, …).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
import contextlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from agent_harness.core.credentials import Credential
from agent_harness.core.models import UsagePricer
from agent_harness.extras.reminders import ReminderQueue
from fastapi import APIRouter, Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles


@dataclass(frozen=True, slots=True)
class TurnProvision:
    """Everything a single chat turn needs from its host."""

    credential: Credential | None = None  # None → the default env credential
    workspace_dir: Path | None = None  # None → the process-wide local workspace
    reminders: ReminderQueue | None = None
    usage_pricer: UsagePricer | None = None
    subscribe_bus: Callable[[Any], Any] | None = None


class TurnWiring(Protocol):
    """Provision a chat turn; the host's per-turn seam.

    An async context manager shape so a host can bracket the streamed run
    (e.g. materialize/flush a remote workspace) — the turn streams inside the
    ``with``; a clean exit is the host's commit point.
    """

    def turn(self, conversation_id: str) -> Any:  # AsyncContextManager[TurnProvision]
        ...


@dataclass(frozen=True, slots=True)
class LocalTurnWiring:
    """Single-player default: env credential, local workspace, DB reminders."""

    @contextlib.asynccontextmanager
    async def turn(self, conversation_id: str) -> AsyncIterator[TurnProvision]:
        from penny.api.persistence.reminders import DbReminderQueue

        yield TurnProvision(reminders=DbReminderQueue())


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Static composition inputs for :func:`create_app`."""

    auth_dependency: Callable[..., Any] | None = None
    turn_wiring: TurnWiring = field(default_factory=LocalTurnWiring)
    extra_routers: Sequence[APIRouter] = ()
    cors_origins: Sequence[str] = ("http://localhost:5173",)
    static_dir: Path | None = None  # built frontend; served at / when set


def create_app(config: AppConfig | None = None) -> FastAPI:
    """Build the chat API app from ``config`` (single-player defaults)."""
    from penny.api import routes
    from penny.bootstrap import bootstrap

    cfg = config or AppConfig()

    @contextlib.asynccontextmanager
    async def _lifespan(app: FastAPI):  # noqa: ANN202
        bootstrap()  # idempotent schema + taxonomy seed
        yield

    app = FastAPI(title="Penny backend", lifespan=_lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(cfg.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )

    dependencies = (
        [Depends(cfg.auth_dependency)] if cfg.auth_dependency is not None else []
    )
    app.include_router(
        routes.build_router(turn_wiring=cfg.turn_wiring), dependencies=dependencies
    )
    for router in cfg.extra_routers:
        app.include_router(router, dependencies=dependencies)

    if cfg.static_dir is not None:
        # The built frontend. html=True serves index.html at /; the SPA's
        # client-side routes (e.g. /c/<id>) fall through to index.html via the
        # 404 handler below rather than a real 404.
        app.mount(
            "/", StaticFiles(directory=cfg.static_dir, html=True), name="frontend"
        )

        @app.exception_handler(404)
        async def _spa_fallback(request: Any, exc: Any):  # noqa: ANN202
            from fastapi.responses import FileResponse, JSONResponse

            if request.url.path.startswith("/api"):
                return JSONResponse({"detail": "Not Found"}, status_code=404)
            index = cfg.static_dir / "index.html"  # type: ignore[operator]
            if index.exists():
                return FileResponse(index)
            return JSONResponse({"detail": "Not Found"}, status_code=404)

    return app
