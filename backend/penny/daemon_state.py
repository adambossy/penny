"""The daemon's job-state file (last run / outcome per job).

Written by the ``penny daemon`` loop after each job; read by ``penny daemon
status`` and the agent's ``sync_status`` tool. Lives apart from
``penny.daemon`` so tool code never imports the scheduler front door.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from penny.workspace import resolve_logs_dir


def state_path() -> Path:
    return resolve_logs_dir() / "daemon-state.json"


def read_state() -> dict[str, Any]:
    path = state_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(state: dict[str, Any]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
