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
    # Cap on the report emails sent in one scheduling pass: when several jobs
    # come due together the highest-priority ones win and the rest resolve
    # without sending — so, in practice, at most this many a day.
    "max_emails_per_day": 1,
    # Hour (New-York local, 0-23) at/after which the daily balance capture
    # runs — ahead of the default 08:00 report slot so a morning report can
    # cite the day's fresh balances.
    "balances_hour": 6,
}

# The cadence fields each period type carries on top of the common ones
# (name / period / hour / priority / recipients), with the default used when
# a hand-edited entry omits one — drives parsing and writing.
_PERIOD_CADENCE_FIELDS: dict[str, dict[str, int]] = {
    "daily": {},
    "weekly": {"weekday": 1},  # ISO weekday: 1=Mon … 7=Sun
    "monthly": {"day_of_month": 1},
    "annual": {"month": 1, "day_of_month": 1},
    "every_n_days": {"n": 7},
}

# What a fresh workspace runs before anyone edits [[jobs]]: one weekly report
# in the default slot (Monday 08:00 local), addressed to whatever
# PENNY_REPORT_RECIPIENTS holds. _normalize_job fills in the rest.
DEFAULT_JOBS: list[dict[str, Any]] = [{"name": "weekly", "period": "weekly"}]


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


def _toml_string(value: str) -> str:
    """One TOML basic string, quoted and escaped."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _normalize_job(raw: dict[str, Any]) -> dict[str, Any]:
    """Coerce one [[jobs]] entry into the shape the daemon consumes.

    Hand-edited TOML is the expected authoring surface, so be tolerant: types
    are coerced and an omitted field falls back to its default. The result is
    total — every field the period needs is present — so nothing downstream
    has to guess. ``recipients`` collapses to None when absent/empty so the
    job falls back to the ambient PENNY_REPORT_RECIPIENTS.
    """
    period = str(raw.get("period", "weekly"))
    recipients: list[str] = []
    if isinstance(raw.get("recipients"), list):
        recipients = [str(r).strip() for r in raw["recipients"] if str(r).strip()]
    job: dict[str, Any] = {
        "name": str(raw.get("name") or period),
        "period": period,
        "hour": int(raw.get("hour", 8)),
        "priority": int(raw.get("priority", 1)),
        "recipients": recipients or None,
    }
    for field, default in _PERIOD_CADENCE_FIELDS.get(period, {}).items():
        job[field] = int(raw.get(field, default))
    return job


def load_jobs() -> list[dict[str, Any]]:
    """The configured report jobs, or the single weekly default when unset.

    A workspace whose ``config.toml`` predates ``[[jobs]]`` may still carry
    its customized weekly slot under the old ``[schedule] report_weekday`` /
    ``report_hour`` keys — those seed the single default job's cadence so
    upgrading never silently resets a customized schedule back to Monday
    08:00.
    """
    config = load_config()
    raw = config.get("jobs")
    entries = raw if isinstance(raw, list) else []
    configured = [job for job in entries if isinstance(job, dict)]
    if configured:
        return [_normalize_job(job) for job in configured]
    schedule = config.get("schedule")
    schedule = schedule if isinstance(schedule, dict) else {}
    legacy_job = dict(DEFAULT_JOBS[0])
    if "report_weekday" in schedule:
        legacy_job["weekday"] = schedule["report_weekday"]
    if "report_hour" in schedule:
        legacy_job["hour"] = schedule["report_hour"]
    return [_normalize_job(legacy_job)]


def write_config(
    env: dict[str, str],
    schedule: dict[str, Any],
    jobs: list[dict[str, Any]],
) -> Path:
    """Write config.toml (chmod 600 — it holds secrets). Returns the path.

    Each job goes out through :func:`_normalize_job`, so what is written is
    exactly what :func:`load_jobs` will read back: only the fields relevant to
    the job's period, and no ``recipients`` line at all when it is None (the
    job keeps falling through to the environment's PENNY_REPORT_RECIPIENTS).
    """
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Penny workspace configuration — written by `penny init`.", ""]
    lines.append("[env]")
    for key in sorted(env):
        lines.append(f"{key} = {_toml_string(env[key])}")
    lines += ["", "[schedule]"]
    lines += [
        f"{key} = {int(schedule.get(key, default))}"
        for key, default in SCHEDULE_DEFAULTS.items()
    ]
    for raw_job in jobs:
        job = _normalize_job(raw_job)
        lines += ["", "[[jobs]]"]
        lines.append(f"name = {_toml_string(job['name'])}")
        lines.append(f"period = {_toml_string(job['period'])}")
        for field in _PERIOD_CADENCE_FIELDS.get(job["period"], {}):
            lines.append(f"{field} = {job[field]}")
        lines.append(f"hour = {job['hour']}")
        if job["recipients"]:
            quoted = ", ".join(_toml_string(addr) for addr in job["recipients"])
            lines.append(f"recipients = [{quoted}]")
        lines.append(f"priority = {job['priority']}")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return path
