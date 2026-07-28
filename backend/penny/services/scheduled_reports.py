"""Schedule selection logic for automated (cron) report runs.

A daily cron run drives the agent with the ``spending-report`` skill, scoped
to the period chosen by New-York wall-clock precedence:

    annual (Jan 1) > monthly (day 1) > weekly (Sunday) > daily

Selection returns a *period* (``daily`` / ``weekly`` / ``monthly`` /
``annual``), and :func:`report_prompt` turns it into a natural-language
request that triggers the period-parameterized ``spending-report`` skill.
There are intentionally no ``report-*`` promptorium keys: the skill is the
single source of report logic, so the four periods cannot drift apart.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from zoneinfo import ZoneInfo

NEW_YORK_TZ = ZoneInfo("America/New_York")

ReportPeriod = Literal["daily", "weekly", "monthly", "annual"]


def _coerce_utc(now_utc: datetime | None) -> datetime:
    """Return an aware UTC datetime for scheduling decisions."""
    if now_utc is None:
        return datetime.now(UTC)

    if now_utc.tzinfo is None:
        return now_utc.replace(tzinfo=UTC)

    return now_utc.astimezone(UTC)


def select_report_period(*, now_utc: datetime | None = None) -> ReportPeriod:
    """Pick the report period with precedence: annual > monthly > weekly > daily.

    The decision is made in New-York local time so a report scheduled for an
    early-morning UTC slot is attributed to the correct local calendar day.
    """
    now = _coerce_utc(now_utc)
    now_ny = now.astimezone(NEW_YORK_TZ)

    if now_ny.month == 1 and now_ny.day == 1:
        return "annual"
    if now_ny.day == 1:
        return "monthly"
    if now_ny.weekday() == 6:
        return "weekly"
    return "daily"


def report_prompt(period: ReportPeriod) -> str:
    """Natural-language request that triggers the ``spending-report`` skill.

    Names the period explicitly so the skill resolves the right window from
    the system prompt's Runtime Context (it never reads a ``report-*`` key), and
    asks for email delivery — the skill (and system prompt) only call
    ``send_email_report`` when the request asks for an emailed report, and the
    recipient is resolved from configuration (never named here).
    Without the explicit "email it to me", a scheduled run would generate the
    report but never send it (exit 0, no email).
    """
    return f"Generate my {period} spending report for the current period and email it to me."
