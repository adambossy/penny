"""``penny daemon`` — the local scheduler for sync and the report jobs.

One long-lived process owning all scheduled work (the local replacement for
the old Fly cron-manager): a Plaid sync every ``sync_interval_hours`` and any
number of configured periodic report jobs (``[[jobs]]`` in the workspace
``config.toml``, see ``penny.settings`` — each with its own period, calendar
slot, recipients, and priority).

Report scheduling is period-identity based, not slot based: a job becomes
eligible when its current period identity (``penny.services
.scheduled_reports.period_identity``) differs from the one last resolved in
daemon state. Each tick the due jobs are ranked by priority and at most
``max_emails_per_day`` of them send; the rest are resolved for the current
period without sending. After an outage spanning several boundaries, only the
single highest-priority still-due job fires — nothing backfills. Only a
successful send (or a deliberate cap-suppression) advances a job's period; a
failed send retries on the next tick.

State (last run / outcome per job) is written to ``logs/daemon-state.json``
in the workspace so ``penny daemon status`` and the agent's ``sync_status``
tool read one place. Jobs are idempotent and skew-tolerant: a missed slot
runs at the next tick, a failed send retries next tick.

Supervision is a user service installed by ``penny init`` (launchd agent on
macOS, systemd user unit on Linux) — see ``penny.service_install``.
"""

from __future__ import annotations

from datetime import UTC, datetime
import os
import subprocess
import time
from typing import Any

from loguru import logger

from penny.daemon_state import read_state, write_state
from penny.service_install import penny_argv
from penny.services.scheduled_reports import is_due_today, period_identity
from penny.settings import load_jobs, load_schedule

_TICK_SECONDS = 60.0


def _run_job(
    state: dict[str, Any],
    name: str,
    argv: list[str],
    *,
    extra_env: dict[str, str] | None = None,
) -> bool:
    """Run one job as a subprocess of this CLI; record outcome in state.

    A subprocess (``penny sync`` / ``penny run-scheduled-report``) keeps each
    job's crash contained: the daemon loop survives anything a job does.
    ``extra_env`` overlays that one subprocess's environment (per-job
    recipients ride in this way) — the daemon's own environ is never mutated.
    """
    started = datetime.now(UTC)
    logger.info("daemon: starting job {}", name)
    try:
        proc = subprocess.run(  # noqa: S603 - argv is our own CLI
            [*penny_argv(), *argv],
            capture_output=True,
            text=True,
            timeout=7200,
            env={**os.environ, **extra_env} if extra_env else None,
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
    write_state(state)
    logger.info("daemon: job {} finished ok={}", name, ok)
    return ok


def _due_sync(state: dict[str, Any], now: datetime, interval_hours: int) -> bool:
    job = state.get("sync", {})
    last = job.get("last_run_at")
    if last is None:
        return True
    if not job.get("ok", True):
        return True  # a failed run retries on the next tick, not next interval
    return (now - datetime.fromisoformat(last)).total_seconds() >= interval_hours * 3600


def _report_state_key(name: str) -> str:
    """Report jobs are namespaced in state so they can never shadow "sync"."""
    return f"report:{name}"


def _report_is_due(
    state: dict[str, Any], job: dict[str, Any], now_utc: datetime
) -> bool:
    """Calendar gate open AND this occurrence of the period not yet resolved.

    A failed send never records the identity, so the job stays due on the next
    tick — success (or cap-suppression) is what advances a job.
    """
    if not is_due_today(job, now_utc=now_utc):
        return False
    recorded = state.get(_report_state_key(job["name"]), {}).get("last_resolved_period")
    return recorded != period_identity(job, now_utc=now_utc)


def _due_reports(
    state: dict[str, Any], now_utc: datetime, jobs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The jobs due this tick, highest priority first (ties keep config order)."""
    due = [job for job in jobs if _report_is_due(state, job, now_utc)]
    return sorted(due, key=lambda job: job["priority"], reverse=True)


def _send_report(state: dict[str, Any], job: dict[str, Any], now_utc: datetime) -> None:
    """Run one report job as a subprocess; only a success spends its period."""
    key = _report_state_key(job["name"])
    recipients = job.get("recipients")
    extra_env = (
        {"PENNY_REPORT_RECIPIENTS": ",".join(recipients)} if recipients else None
    )
    ok = _run_job(
        state,
        key,
        ["run-scheduled-report", "--job", job["name"]],
        extra_env=extra_env,
    )
    if ok:
        state[key]["last_resolved_period"] = period_identity(job, now_utc=now_utc)
        write_state(state)


def _resolve_without_sending(
    state: dict[str, Any], job: dict[str, Any], now_utc: datetime, detail: str
) -> None:
    """Spend a job's current period without running it (cap suppression)."""
    state[_report_state_key(job["name"])] = {
        "last_run_at": datetime.now(UTC).isoformat(),
        "ok": True,
        "detail": detail,
        "last_resolved_period": period_identity(job, now_utc=now_utc),
    }


def _tick_reports(
    state: dict[str, Any],
    now_utc: datetime,
    jobs: list[dict[str, Any]],
    max_emails_per_day: int,
) -> None:
    """One scheduling pass: send the top jobs, resolve the suppressed rest.

    Suppressed jobs are resolved for their *current* period only — after an
    outage they lose exactly the occurrence they lost the slot for, and come
    due again at their next natural boundary; nothing retro-sends.
    """
    due = _due_reports(state, now_utc, jobs)
    winners, suppressed = due[:max_emails_per_day], due[max_emails_per_day:]
    for job in winners:
        _send_report(state, job, now_utc)
    if not suppressed:
        return
    detail = (
        f"resolved without sending: suppressed by "
        f"max_emails_per_day={max_emails_per_day} "
        f"({', '.join(job['name'] for job in winners)} won)"
    )
    for job in suppressed:
        _resolve_without_sending(state, job, now_utc, detail)
    write_state(state)


def run_daemon() -> None:
    """The scheduler loop. Blocks forever; the service manager supervises."""
    schedule = load_schedule()
    jobs = load_jobs()
    logger.info(
        "penny daemon up: sync every {}h; report jobs [{}], max {} email(s)/day",
        schedule["sync_interval_hours"],
        ", ".join(job["name"] for job in jobs),
        schedule["max_emails_per_day"],
    )
    while True:
        state = read_state()
        now = datetime.now(UTC)
        if _due_sync(state, now, schedule["sync_interval_hours"]):
            _run_job(state, "sync", ["sync"])
        _tick_reports(state, now, jobs, schedule["max_emails_per_day"])
        time.sleep(_TICK_SECONDS)
