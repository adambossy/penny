"""The default single-player app instance (``uvicorn penny.api.main:app``).

Single-player: there is no auth, no billing, no tenancy — every request is
the one local user. The app binds to localhost by default (``penny serve``);
remote access is the user's business via Tailscale (blessed) or ngrok
(warned), never an auth layer here.

Hosting products compose their own instance via :func:`penny.api.app.create_app`
instead of importing this module.
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv(override=False)
from penny.settings import apply_config_to_env  # noqa: E402

apply_config_to_env()
# Import _logging first so the file sink is installed before anything
# downstream emits its first log line.
from penny import _logging  # noqa: E402, F401  side-effect: install file sink
from penny.api.app import create_app  # noqa: E402
from penny.observability import init_sentry  # noqa: E402

# Initialize error tracking before the app is built so startup and
# request-handler failures are reported. Idempotent + no-op when unconfigured.
init_sentry()

app = create_app()
