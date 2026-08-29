"""The ``[[jobs]]`` half of the workspace config.toml contract.

``write_config`` and ``load_jobs`` are two halves of one file format, so the
round trip is the thing worth pinning: what the wizard writes is what the
daemon reads back, and a hand-edited entry that omits fields still normalizes
to a job the daemon can run without guessing.
"""

from __future__ import annotations

from pathlib import Path

from penny.settings import load_jobs, write_config


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
