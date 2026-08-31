"""The default single-player app instance (``uvicorn penny.api.main:app``).

Single-player: there is no auth, no billing, no tenancy — every request is
the one local user. The app binds to localhost by default (``penny serve``);
remote access is the user's business via Tailscale (blessed) or ngrok
(warned), never an auth layer here.

Hosting products compose their own instance via :func:`penny.api.app.create_app`
instead of importing this module.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_had_real_workspace_env = "PENNY_WORKSPACE" in os.environ
load_dotenv(override=False)

# backend/.env is symlinked into every worktree by the WorktreeCreate hook
# (single source of truth for secrets), so a PENNY_WORKSPACE set there is
# identical everywhere too — every worktree ends up sharing one workspace.
# .penny-workspace is a per-worktree, gitignored, un-symlinked (the hook
# only links .env / .env.* / *.env) escape hatch: drop a
# PENNY_WORKSPACE=<path> line in it to give one worktree its own
# memory/reports/config.toml without touching the shared secrets file.
# Only allowed to beat .env's own PENNY_WORKSPACE, never a real environment
# variable set before this module was imported (e.g. a test harness's
# redirect) — that must always win, per the project's env-contract rule.
if not _had_real_workspace_env:
    load_dotenv(Path(__file__).resolve().parents[2] / ".penny-workspace", override=True)
from penny.settings import apply_config_to_env  # noqa: E402

apply_config_to_env()
# Import _logging first so the file sink is installed before anything
# downstream emits its first log line.
from penny import _logging  # noqa: E402, F401  side-effect: install file sink
from penny.api.app import AppConfig, create_app, default_static_dir  # noqa: E402
from penny.observability import init_sentry  # noqa: E402

# Initialize error tracking before the app is built so startup and
# request-handler failures are reported. Idempotent + no-op when unconfigured.
init_sentry()

# Serve the built web UI when there is one, exactly as `penny serve` does.
# Without this the two front doors disagree: `uvicorn penny.api.main:app` --
# the documented dev-loop command -- answered /api but 404'd `/` and every SPA
# route, which reads as "the app is down" rather than "the UI isn't mounted".
app = create_app(AppConfig(static_dir=default_static_dir()))
