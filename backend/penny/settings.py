"""Workspace ``config.toml`` — the persistent home of ``penny init``'s answers.

One file, one direction: the wizard writes it; every entrypoint loads it into
the environment (defaults only — a real environment variable always wins, so
the ``PENNY_*`` env contract stays the single runtime seam and nothing else
in the codebase learns about the file). Secrets live in it chmod 600.

The [env] table maps verbatim onto environment variables. The [schedule]
table holds the daemon's globals (sync interval, email cap), read through
:func:`load_schedule`; the [[jobs]] array-of-tables holds the per-job report
cadence (period, calendar slot, recipients, priority), read through
:func:`load_jobs`.
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

# The daemon's global cadence defaults — one home, consumed by both
# load_schedule and write_config so the wizard and the daemon can't disagree.
SCHEDULE_DEFAULTS: dict[str, int] = {
    "sync_interval_hours": 12,
    # At most this many report-job emails per calendar day; when several jobs
    # come due together the highest-priority one wins the slot.
    "max_emails_per_day": 1,
}

# What a fresh workspace runs before anyone edits [[jobs]]: one weekly report,
# Monday 08:00 local. recipients=None means "leave PENNY_REPORT_RECIPIENTS to
# the ambient environment" (the wizard-stored [env] default).
DEFAULT_JOBS: list[dict[str, Any]] = [
    {
        "name": "weekly",
        "period": "weekly",
        "weekday": 1,
        "hour": 8,
        "recipients": None,
        "priority": 1,
    }
]

# Which cadence fields each period type carries (beyond the common
# name/period/hour/recipients/priority) — drives both parsing and writing.
_PERIOD_CADENCE_FIELDS: dict[str, tuple[str, ...]] = {
    "daily": (),
    "weekly": ("weekday",),
    "monthly": ("day_of_month",),
    "annual": ("month", "day_of_month"),
    "every_n_days": ("n",),
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
    """The daemon's globals: sync interval + daily email cap (with defaults)."""
    schedule = load_config().get("schedule")
    schedule = schedule if isinstance(schedule, dict) else {}
    return {
        key: int(schedule.get(key, default))
        for key, default in SCHEDULE_DEFAULTS.items()
    }


def _normalize_job(raw: dict[str, Any]) -> dict[str, Any]:
    """Coerce one [[jobs]] entry into the shape the daemon consumes.

    Hand-edited TOML is the expected authoring surface, so be tolerant about
    types (ints arrive as ints, but normalize anyway) while keeping the field
    set explicit. ``recipients`` collapses to None when absent/empty so the
    job falls back to the ambient PENNY_REPORT_RECIPIENTS.
    """
    period = str(raw.get("period", "weekly"))
    job: dict[str, Any] = {
        "name": str(raw.get("name") or period),
        "period": period,
        "hour": int(raw.get("hour", 8)),
        "priority": int(raw.get("priority", 1)),
        "recipients": None,
    }
    recipients = raw.get("recipients")
    if isinstance(recipients, list):
        cleaned = [str(r).strip() for r in recipients if str(r).strip()]
        job["recipients"] = cleaned or None
    for field in _PERIOD_CADENCE_FIELDS.get(period, ()):
        if field in raw:
            job[field] = int(raw[field])
    return job


def load_jobs() -> list[dict[str, Any]]:
    """The configured report jobs, or the single weekly default when unset."""
    raw = load_config().get("jobs")
    if not isinstance(raw, list):
        raw = []
    jobs = [_normalize_job(entry) for entry in raw if isinstance(entry, dict)]
    return jobs or [dict(job) for job in DEFAULT_JOBS]


def write_config(
    env: dict[str, str],
    schedule: dict[str, Any],
    jobs: list[dict[str, Any]] | None = None,
) -> Path:
    """Write config.toml (chmod 600 — it holds secrets). Returns the path.

    Only the fields relevant to each job's period are emitted; a None
    ``recipients`` is skipped entirely so the job keeps falling through to
    the environment's PENNY_REPORT_RECIPIENTS.
    """
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
    for job in jobs if jobs is not None else DEFAULT_JOBS:
        lines += ["", "[[jobs]]"]
        lines.append(f'name = "{job["name"]}"')
        lines.append(f'period = "{job["period"]}"')
        for field in _PERIOD_CADENCE_FIELDS.get(job["period"], ()):
            if job.get(field) is not None:
                lines.append(f"{field} = {int(job[field])}")
        lines.append(f"hour = {int(job.get('hour', 8))}")
        recipients = job.get("recipients")
        if recipients:
            quoted = ", ".join(f'"{addr}"' for addr in recipients)
            lines.append(f"recipients = [{quoted}]")
        lines.append(f"priority = {int(job.get('priority', 1))}")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return path
