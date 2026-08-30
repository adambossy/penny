"""The ``[[jobs]]`` half of the workspace config.toml contract.

``write_config`` and ``load_jobs`` are two halves of one file format, so the
round trip is the thing worth pinning: what the wizard writes is what the
daemon reads back, and a hand-edited entry that omits fields still normalizes
to a job the daemon can run without guessing.
"""

from __future__ import annotations

from pathlib import Path

from penny.settings import load_jobs, load_schedule, write_config


def test_write_config_round_trips_jobs(isolated_workspace: Path) -> None:
    # input: the shape the wizard/`transient` scripts hand in.
    jobs = [
        {"name": "daily", "period": "daily", "hour": 7, "recipients": ["me@x.com"]},
        {
            "name": "annual",
            "period": "annual",
            "month": 1,
            "day_of_month": 1,
            "hour": 9,
            "recipients": ["me@x.com", "you@x.com"],
            "priority": 4,
        },
    ]

    # act
    write_config({}, {"sync_interval_hours": 6}, jobs)
    output = load_jobs()

    # assert: every field survives the file, including the per-job recipients.
    assert output == [
        {
            "name": "daily",
            "period": "daily",
            "hour": 7,
            "priority": 1,
            "recipients": ["me@x.com"],
        },
        {
            "name": "annual",
            "period": "annual",
            "hour": 9,
            "priority": 4,
            "recipients": ["me@x.com", "you@x.com"],
            "month": 1,
            "day_of_month": 1,
        },
    ]


def test_hand_edited_job_fills_in_the_periods_defaults(
    isolated_workspace: Path,
) -> None:
    # input: a minimal hand-edited entry — no slot, no recipients, no priority.
    (isolated_workspace / "config.toml").write_text(
        '[[jobs]]\nname = "catch-up"\nperiod = "weekly"\n', encoding="utf-8"
    )

    # act
    output = load_jobs()

    # assert: normalized to a complete job (Monday 08:00, ambient recipients)
    # so the daemon's gates never meet a missing field.
    assert output == [
        {
            "name": "catch-up",
            "period": "weekly",
            "hour": 8,
            "priority": 1,
            "recipients": None,
            "weekday": 1,
        }
    ]


def test_no_jobs_table_falls_back_to_the_weekly_default(
    isolated_workspace: Path,
) -> None:
    # input: a config with no [[jobs]] at all (pre-jobs workspace).
    (isolated_workspace / "config.toml").write_text(
        "[schedule]\nsync_interval_hours = 12\n", encoding="utf-8"
    )

    # assert
    assert [job["name"] for job in load_jobs()] == ["weekly"]


def test_legacy_schedule_slot_seeds_the_default_weekly_job(
    isolated_workspace: Path,
) -> None:
    # input: a pre-[[jobs]] workspace with a customized weekly slot — the
    # shape `penny init` used to write before the [[jobs]] schedule existed.
    (isolated_workspace / "config.toml").write_text(
        "[schedule]\nsync_interval_hours = 12\nreport_weekday = 5\nreport_hour = 18\n",
        encoding="utf-8",
    )

    # act
    output = load_jobs()

    # assert: the customized slot survives instead of resetting to Mon 08:00.
    assert output == [
        {
            "name": "weekly",
            "period": "weekly",
            "hour": 18,
            "priority": 1,
            "recipients": None,
            "weekday": 5,
        }
    ]


def test_schedule_round_trips_the_balances_hour(isolated_workspace: Path) -> None:
    # input: a customized capture hour (hand-edited; init never prompts for it).
    write_config({}, {"sync_interval_hours": 6, "balances_hour": 5}, [])

    # act
    output = load_schedule()

    # assert: the hour survives the file, and the defaults fill the rest.
    assert output["balances_hour"] == 5
    assert output["sync_interval_hours"] == 6
    assert output["max_emails_per_day"] == 1
