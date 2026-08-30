"""Period identity, calendar gates, and prompt text for report jobs.

The identity string is the daemon's memory of "already handled this period";
these tests pin its shape per period type, the New-York day attribution, and
the every_n_days bucketing (stable within a bucket, rolls after n days).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from penny.services.scheduled_reports import (
    is_due_today,
    period_identity,
    report_prompt,
)


def _at(iso: str) -> datetime:
    """Build an aware UTC datetime from an ISO string."""
    return datetime.fromisoformat(iso).replace(tzinfo=UTC)


# Saturday 2026-08-29, noon in New York (16:00 UTC during EDT).
_SATURDAY_NOON = _at("2026-08-29T16:00:00")


@pytest.mark.parametrize(
    "job,expected_identity",
    [
        ({"period": "daily"}, "2026-08-29"),
        ({"period": "weekly", "weekday": 6}, "2026-W35"),
        ({"period": "monthly", "day_of_month": 29}, "2026-08"),
        ({"period": "annual", "month": 8, "day_of_month": 29}, "2026"),
        # (2026-08-29 - 2026-01-01) = 240 days -> bucket 240 // 2.
        ({"period": "every_n_days", "n": 2}, "120"),
    ],
)
def test_period_identity_per_period_type(job: dict, expected_identity: str) -> None:
    # act
    output = period_identity(job, now_utc=_SATURDAY_NOON)

    # assert
    assert output == expected_identity


def test_period_identity_uses_new_york_calendar_day() -> None:
    # input: 2026-01-01 02:00 UTC is still 2025-12-31 21:00 in New York, so
    # both the daily and annual identities belong to the OLD period.
    now_utc = _at("2026-01-01T02:00:00")

    # assert
    assert period_identity({"period": "daily"}, now_utc=now_utc) == "2025-12-31"
    assert period_identity({"period": "annual"}, now_utc=now_utc) == "2025"


def test_every_n_days_identity_is_stable_then_rolls_after_n_days() -> None:
    # input
    job = {"period": "every_n_days", "n": 2}

    # act: same date twice, the next day (same bucket), then day n
    same = period_identity(job, now_utc=_SATURDAY_NOON)
    again = period_identity(job, now_utc=_SATURDAY_NOON)
    next_day = period_identity(job, now_utc=_at("2026-08-30T16:00:00"))
    after_n = period_identity(job, now_utc=_at("2026-08-31T16:00:00"))

    # assert: deterministic across calls, unchanged within the bucket,
    # different once n days have elapsed.
    assert same == again == next_day == "120"
    assert after_n == "121"


def test_unknown_period_raises() -> None:
    with pytest.raises(ValueError):
        period_identity({"period": "fortnightly"}, now_utc=_SATURDAY_NOON)
    with pytest.raises(ValueError):
        is_due_today({"period": "fortnightly", "hour": 8}, now_utc=_SATURDAY_NOON)


@pytest.mark.parametrize(
    "job,expected_due",
    [
        # daily / every_n_days: the day gate is always open (identity carries
        # the cadence); only the hour gates.
        ({"period": "daily", "hour": 8}, True),
        ({"period": "daily", "hour": 13}, False),
        ({"period": "every_n_days", "n": 3, "hour": 8}, True),
        # weekly: 2026-08-29 is a Saturday (ISO weekday 6).
        ({"period": "weekly", "weekday": 6, "hour": 8}, True),
        ({"period": "weekly", "weekday": 1, "hour": 8}, False),
        # monthly: day 29.
        ({"period": "monthly", "day_of_month": 29, "hour": 8}, True),
        ({"period": "monthly", "day_of_month": 1, "hour": 8}, False),
        # annual: Aug 29.
        ({"period": "annual", "month": 8, "day_of_month": 29, "hour": 8}, True),
        ({"period": "annual", "month": 1, "day_of_month": 1, "hour": 8}, False),
    ],
)
def test_is_due_today_calendar_and_hour_gates(job: dict, expected_due: bool) -> None:
    assert is_due_today(job, now_utc=_SATURDAY_NOON) is expected_due


def test_is_due_today_fires_for_the_rest_of_the_day() -> None:
    # The gate is >= hour, not == hour: a laptop opened at 22:00 still runs
    # the 08:00 job that day (the identity check stops repeats).
    late = _at("2026-08-30T02:00:00")  # 22:00 Saturday in New York
    assert is_due_today({"period": "daily", "hour": 8}, now_utc=late)


@pytest.mark.parametrize(
    "period,expected_window",
    [
        ("daily", "today"),
        ("weekly", "this week"),
        ("monthly", "this month"),
        ("annual", "this year"),
    ],
)
def test_report_prompt_names_period_and_window(
    period: str, expected_window: str
) -> None:
    # act
    output = report_prompt({"period": period})

    # expected: names the period + window for the spending-report skill,
    # explicitly asks for email delivery — the skill only calls
    # send_email_report when the request asks (see commit 1696ebf) — and
    # states this is unattended so the agent resolves stale data itself
    # instead of asking a question no one will answer (an observed run
    # asked "shall I sync now?" and silently never sent).
    expected_output = (
        f"Generate my {period} spending report covering {expected_window} "
        "and email it to me. This is an unattended scheduled run — there "
        "is no one available to answer a question, so do not ask for "
        "confirmation or wait for a reply. If the current period has no "
        "data yet, sync first; if data is still stale afterward, report "
        "on the most recent available period instead and note the "
        "staleness. Always finish by generating and emailing a report — "
        "never end by asking a question."
    )

    # assert
    assert output == expected_output


def test_report_prompt_every_n_days_spells_out_the_window() -> None:
    # act
    output = report_prompt({"period": "every_n_days", "n": 2})

    # assert: no awkward "every_n_days" in the prose, still asks for email
    # and states the unattended-run guardrail.
    assert output == (
        "Generate my spending report covering the last 2 days and email it "
        "to me. This is an unattended scheduled run — there is no one "
        "available to answer a question, so do not ask for confirmation or "
        "wait for a reply. If the current period has no data yet, sync "
        "first; if data is still stale afterward, report on the most "
        "recent available period instead and note the staleness. Always "
        "finish by generating and emailing a report — never end by asking "
        "a question."
    )
