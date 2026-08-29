"""Mirror loguru output to a rotating log file under the workspace.

Loguru ships with a stderr sink; this module adds a file sink at
``~/.penny/logs/penny.log`` (or wherever ``PENNY_WORKSPACE`` points)
alongside it. Importing this module is the install — it's idempotent, so
multiple imports are harmless.
"""

from __future__ import annotations

from loguru import logger

from .workspace import resolve_logs_dir

_installed = False


def _install() -> None:
    global _installed
    if _installed:
        return
    logs_dir = resolve_logs_dir()
    logs_dir.mkdir(parents=True, exist_ok=True)
    logger.add(
        logs_dir / "penny.log",
        rotation="10 MB",
        retention=5,
        compression="gz",
        level="DEBUG",
        backtrace=True,
        diagnose=False,  # don't include local values — log files can leak
    )
    _installed = True


_install()
