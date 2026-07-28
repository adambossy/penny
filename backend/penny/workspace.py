"""Workspace directory resolution.

The workspace directory holds user-specific runtime data: ``memory/``,
``reports/``, ``logs/``, the minted ``user_id``, and ``config.toml`` (see
``penny.settings``). The default location is ``~/.penny``; an existing
``~/.transactoid`` (the prior product name) is honored so old user state
carries over without migration. Override with ``$PENNY_WORKSPACE``.
"""

from __future__ import annotations

import os
from pathlib import Path

PENNY_WORKSPACE = "PENNY_WORKSPACE"

_DEFAULT_WORKSPACE = Path.home() / ".penny"
_LEGACY_WORKSPACE = Path.home() / ".transactoid"


def resolve_workspace_dir() -> Path:
    """Return the workspace root directory.

    ``$PENNY_WORKSPACE`` wins when set. Otherwise ``~/.penny`` — unless it
    does not exist and the legacy ``~/.transactoid`` does, in which case the
    legacy directory is used as-is (existing state keeps working; ``penny
    init`` offers the actual move).
    """
    env_value = os.environ.get(PENNY_WORKSPACE, "").strip()
    if env_value:
        return Path(env_value).expanduser()
    if not _DEFAULT_WORKSPACE.exists() and _LEGACY_WORKSPACE.exists():
        return _LEGACY_WORKSPACE
    return _DEFAULT_WORKSPACE


def resolve_memory_dir() -> Path:
    """Return the ``memory/`` subdirectory inside the workspace."""
    return resolve_workspace_dir() / "memory"


def resolve_reports_dir() -> Path:
    """Return the ``reports/`` subdirectory inside the workspace."""
    return resolve_workspace_dir() / "reports"


def resolve_logs_dir() -> Path:
    """Return the ``logs/`` subdirectory inside the workspace."""
    return resolve_workspace_dir() / "logs"
