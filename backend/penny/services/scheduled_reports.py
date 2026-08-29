"""Period identity + calendar gates for the daemon's scheduled report jobs.

Each configured job (``[[jobs]]`` in the workspace config.toml, see
``penny.settings``) names a period type; this module answers two questions
about a job, both in New-York wall-clock time:

- :func:`is_due_today` — does the job's calendar gate (weekday / day of
  month / hour) match right now?
- :func:`period_identity` — an opaque string that is stable within one
  occurrence of the job's period and changes when the period rolls over.
  The daemon records the identity it last resolved, so "already handled
  this period" survives restarts and laptop sleep without slot arithmetic.

:func:`report_prompt` turns a job's period into a natural-language request
that triggers the period-parameterized ``spending-report`` skill. There are
intentionally no ``report-*`` promptorium keys: the skill is the single
source of report logic, so the periods cannot drift apart.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

NEW_YORK_TZ = ZoneInfo("America/New_York")

# Fixed reference for every_n_days bucketing — identities must be
# deterministic across process restarts, so the epoch can never move.
EVERY_N_DAYS_EPOCH = date(2026, 1, 1)

_WINDOWS = {
    "daily": "today",
    "weekly": "this week",
    "monthly": "this month",
    "annual": "this year",
}


def _now_ny(now_utc: datetime | None) -> datetime:
    """New-York wall-clock time for scheduling decisions (naive input = UTC)."""
    if now_utc is None:
        now_utc = datetime.now(UTC)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=UTC)
    return now_utc.astimezone(NEW_YORK_TZ)


def period_identity(job: dict[str, Any], *, now_utc: datetime | None = None) -> str:
    """The job's current period occurrence, as an opaque stable string.

    Same string for every tick inside one period, a different string once the
    period rolls over — that transition (not elapsed time) is what makes a
    job eligible again.
    """
    today = _now_ny(now_utc).date()
    period = job["period"]
    if period == "daily":
        return today.isoformat()
    if period == "weekly":
        iso_year, iso_week, _ = today.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    if period == "monthly":
        return f"{today.year}-{today.month:02d}"
    if period == "annual":
        return str(today.year)
    if period == "every_n_days":
        n = int(job["n"])
        return str((today.toordinal() - EVERY_N_DAYS_EPOCH.toordinal()) // n)
    raise ValueError(f"unknown report period: {period!r}")


def is_due_today(job: dict[str, Any], *, now_utc: datetime | None = None) -> bool:
    """The job's calendar gate: right day (NY time) and at/past its hour.

    daily / every_n_days pass the day gate always — their cadence is carried
    entirely by :func:`period_identity` rolling over.
    """
    now_ny = _now_ny(now_utc)
    period = job["period"]
    if period in ("daily", "every_n_days"):
        day_matches = True
    elif period == "weekly":
        day_matches = now_ny.isoweekday() == int(job["weekday"])
    elif period == "monthly":
        day_matches = now_ny.day == int(job["day_of_month"])
    elif period == "annual":
        day_matches = now_ny.month == int(job["month"]) and now_ny.day == int(
            job["day_of_month"]
        )
    else:
        raise ValueError(f"unknown report period: {period!r}")
    return day_matches and now_ny.hour >= int(job["hour"])


def period_window(job: dict[str, Any]) -> str:
    """The content window the report should cover, as prompt-ready English."""
    if job["period"] == "every_n_days":
        return f"the last {int(job['n'])} days"
    return _WINDOWS[job["period"]]


def report_prompt(period: str, window: str) -> str:
    """Natural-language request that triggers the ``spending-report`` skill.

    Names the period and window explicitly so the skill resolves the right
    range from the system prompt's Runtime Context (it never reads a
    ``report-*`` key), and asks for email delivery — the skill (and system
    prompt) only call ``send_email_report`` when the request asks for an
    emailed report, and the recipient is resolved from configuration (never
    named here). Without the explicit "email it to me", a scheduled run would
    generate the report but never send it (exit 0, no email).
    """
    if period == "every_n_days":
        return f"Generate my spending report covering {window} and email it to me."
    return f"Generate my {period} spending report covering {window} and email it to me."
