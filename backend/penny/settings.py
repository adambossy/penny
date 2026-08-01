"""Workspace ``config.toml`` — the persistent home of ``penny init``'s answers.

One file, one direction: the wizard writes it; every entrypoint loads it into
the environment (defaults only — a real environment variable always wins, so
the ``PENNY_*`` env contract stays the single runtime seam and nothing else
in the codebase learns about the file). Secrets live in it chmod 600.

The [env] table maps verbatim onto environment variables. The [schedule]
table is the daemon's cadence config, read through :func:`load_schedule`.
"""

from __future__ import annotations

import os
from pathlib import Path
import stat
import tomllib
from typing import Any

from loguru import logger

from penny.workspace import resolve_workspace_dir

_CONFIG_NAME = "config.toml"

# The daemon cadence defaults — one home, consumed by both load_schedule and
# write_config so the wizard and the daemon can't disagree.
SCHEDULE_DEFAULTS: dict[str, int] = {
    "sync_interval_hours": 12,
    # Weekly report: ISO weekday (1=Mon … 7=Sun) + local hour.
    "report_weekday": 1,
    "report_hour": 8,
}


def config_path() -> Path:
    return resolve_workspace_dir() / _CONFIG_NAME


def load_config() -> dict[str, Any]:
    """Parse the workspace config.toml ({} when absent or unreadable).

    An unreadable/corrupt file degrades to {} so every entrypoint still
    starts, but loudly — this file holds the user's keys, and silently
    dropping them would surface only as confusing downstream failures.
    """
    path = config_path()
    if not path.exists():
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.warning(
            "Ignoring unreadable workspace config {} ({}); re-run `penny init` "
            "to rewrite it.",
            path,
            exc,
        )
        return {}


def apply_config_to_env() -> None:
    """Apply the config's [env] table as environment DEFAULTS.

    ``os.environ.setdefault`` — a variable already present in the real
    environment always wins, so ad-hoc overrides (and tests) behave normally.
    """
    env = load_config().get("env")
    if not isinstance(env, dict):
        return
    for key, value in env.items():
        if isinstance(value, str | int | float | bool):
            os.environ.setdefault(str(key), str(value))


def load_schedule() -> dict[str, Any]:
    """The daemon cadence: sync interval + weekly report slot (with defaults)."""
    schedule = load_config().get("schedule")
    schedule = schedule if isinstance(schedule, dict) else {}
    return {
        key: int(schedule.get(key, default))
        for key, default in SCHEDULE_DEFAULTS.items()
    }


def write_config(env: dict[str, str], schedule: dict[str, Any]) -> Path:
    """Write config.toml (chmod 600 — it holds secrets). Returns the path."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Penny workspace configuration — written by `penny init`.", ""]
    lines.append("[env]")
    for key in sorted(env):
        value = env[key].replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'{key} = "{value}"')
    lines += ["", "[schedule]"]
    lines += [
        f"{key} = {int(schedule.get(key, default))}"
        for key, default in SCHEDULE_DEFAULTS.items()
    ]
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return path
