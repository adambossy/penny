"""``penny daemon`` — the local scheduler for sync and the weekly report.

One long-lived process owning all scheduled work (the local replacement for
the old Fly cron-manager): a Plaid sync every ``sync_interval_hours`` and the
weekly emailed spending report at the configured local weekday/hour. Cadence
comes from the workspace ``config.toml`` ([schedule], see ``penny.settings``).

State (last run / outcome per job) is written to ``logs/daemon-state.json``
in the workspace so ``penny daemon status`` and the agent's ``sync_status``
tool read one place. Jobs are idempotent and skew-tolerant: a missed slot
runs at the next tick, a failed send retries next tick.

Supervision is a user service installed by ``penny init`` (launchd agent on
macOS, systemd user unit on Linux) — see ``penny.service_install``.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

from loguru import logger

from penny.settings import load_schedule
from penny.workspace import resolve_logs_dir

_TICK_SECONDS = 60.0


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


def _write_state(state: dict[str, Any]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


def _run_job(state: dict[str, Any], name: str, argv: list[str]) -> None:
    """Run one job as a subprocess of this CLI; record outcome in state.

    A subprocess (``penny sync`` / ``penny run-scheduled-report``) keeps each
    job's crash contained: the daemon loop survives anything a job does.
    """
    started = datetime.now(UTC)
    logger.info("daemon: starting job {}", name)
    try:
        proc = subprocess.run(  # noqa: S603 - argv is our own CLI
            [sys.executable, "-m", "penny.cli", *argv],
            capture_output=True,
            text=True,
            timeout=7200,
        )
        ok = proc.returncode == 0
        detail = (proc.stdout or "")[-2000:] if ok else (proc.stderr or "")[-2000:]
    except Exception as exc:  # noqa: BLE001 - the loop must survive any job
        ok, detail = False, str(exc)
    state[name] = {
        "last_run_at": started.isoformat(),
        "ok": ok,
        "detail": detail.strip(),
    }
    _write_state(state)
    logger.info("daemon: job {} finished ok={}", name, ok)


def _due_sync(state: dict[str, Any], now: datetime, interval_hours: int) -> bool:
    last = state.get("sync", {}).get("last_run_at")
    if last is None:
        return True
    return (now - datetime.fromisoformat(last)).total_seconds() >= interval_hours * 3600


def _due_report(state: dict[str, Any], now_local: datetime, schedule: dict) -> bool:
    """True in the configured weekly slot, at most once per slot.

    The slot is (weekday, hour) local time; "once" is enforced by comparing
    the last report run's date to today's.
    """
    if now_local.isoweekday() != schedule["report_weekday"]:
        return False
    if now_local.hour < schedule["report_hour"]:
        return False
    last = state.get("report", {}).get("last_run_at")
    if last is None:
        return True
    return datetime.fromisoformat(last).astimezone().date() < now_local.date()


def run_daemon() -> None:
    """The scheduler loop. Blocks forever; the service manager supervises."""
    schedule = load_schedule()
    logger.info(
        "penny daemon up: sync every {}h; report weekday={} hour={}",
        schedule["sync_interval_hours"],
        schedule["report_weekday"],
        schedule["report_hour"],
    )
    while True:
        state = read_state()
        now = datetime.now(UTC)
        if _due_sync(state, now, schedule["sync_interval_hours"]):
            _run_job(state, "sync", ["sync"])
        state = read_state()
        if _due_report(state, datetime.now().astimezone(), schedule):
            _run_job(state, "report", ["run-scheduled-report"])
        time.sleep(_TICK_SECONDS)
