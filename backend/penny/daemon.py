"""``penny daemon`` — the local scheduler for sync and the report jobs.

One long-lived process owning all scheduled work (the local replacement for
the old Fly cron-manager): a Plaid sync every ``sync_interval_hours``, a
daily balance capture at/after ``balances_hour`` (New-York local, like the
report slots), and any number of configured periodic report jobs (``[[jobs]]``
in the workspace ``config.toml``, see ``penny.settings`` — each with its own
period, calendar slot, recipients, and priority).

Report scheduling is period-identity based, not slot based: a job becomes
eligible when its current period identity (``penny.services
.scheduled_reports.period_identity``) differs from the one last resolved in
daemon state, and each tick the highest-priority ``max_emails_per_day`` due
jobs send while the rest resolve their period without sending. REQUIREMENTS
P4 holds the full policy (no backfill after an outage; only success or
cap-suppression advances a job, so a failed send retries next tick).

State (last run / outcome per job) is written to ``logs/daemon-state.json``
in the workspace so ``penny daemon status`` and the agent's ``sync_status``
tool read one place. Jobs are idempotent and skew-tolerant: a missed slot
runs at the next tick, a failed send retries next tick.

Supervision is a user service installed by ``penny init`` (launchd agent on
macOS, systemd user unit on Linux) — see ``penny.service_install``.
"""

from __future__ import annotations

from datetime import UTC, datetime
import subprocess
import time
from typing import Any

from loguru import logger

from penny.daemon_state import REPORT_STATE_PREFIX, read_state, write_state
from penny.service_install import penny_argv
from penny.services.scheduled_reports import is_due_today, period_identity
from penny.settings import load_jobs, load_schedule

_TICK_SECONDS = 60.0


def _run_job(state: dict[str, Any], name: str, argv: list[str]) -> bool:
    """Run one job as a subprocess of this CLI; record outcome in state.

    A subprocess (``penny sync`` / ``penny run-scheduled-report``) keeps each
    job's crash contained: the daemon loop survives anything a job does.
    """
    started = datetime.now(UTC)
    logger.info("daemon: starting job {}", name)
    try:
        proc = subprocess.run(  # noqa: S603 - argv is our own CLI
            [*penny_argv(), *argv],
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
    return f"{REPORT_STATE_PREFIX}{name}"


# The daily balance capture, phrased as a report-shaped job (minus a state
# namespace and the email cap) so the calendar gate, period identity, and
# only-success-spends-the-period semantics all come from the one periodic
# machine below — New-York wall clock and all.
def _balances_job(hour: int) -> dict[str, Any]:
    return {"period": "daily", "hour": hour}


def _periodic_is_due(
    state: dict[str, Any], key: str, job: dict[str, Any], now_utc: datetime
) -> bool:
    """Calendar gate open AND this occurrence of the period not yet resolved.

    A failed run never records the identity, so the job stays due on the next
    tick — success (or cap-suppression) is what advances a job.
    """
    if not is_due_today(job, now_utc=now_utc):
        return False
    recorded = state.get(key, {}).get("last_resolved_period")
    return recorded != period_identity(job, now_utc=now_utc)


def _run_periodic(
    state: dict[str, Any],
    key: str,
    job: dict[str, Any],
    argv: list[str],
    now_utc: datetime,
) -> None:
    """Run one periodic job as a subprocess; only a success spends its period.

    ``_run_job`` has already written the bare "ok" record to disk before this
    function ever sees it, so a success's resolved period is persisted
    immediately here too — otherwise a crash right after this call returns
    would leave the run recorded as ok yet still due, and the job would run
    again on restart.
    """
    ok = _run_job(state, key, argv)
    if ok:
        state[key]["last_resolved_period"] = period_identity(job, now_utc=now_utc)
        write_state(state)


def _due_reports(
    state: dict[str, Any], now_utc: datetime, jobs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The jobs due this tick, highest priority first (ties keep config order)."""
    due = [
        job
        for job in jobs
        if _periodic_is_due(state, _report_state_key(job["name"]), job, now_utc)
    ]
    return sorted(due, key=lambda job: job["priority"], reverse=True)


def _send_report(state: dict[str, Any], job: dict[str, Any], now_utc: datetime) -> None:
    """The job subprocess resolves its own recipients from the ``[[jobs]]``
    config, so the daemon passes nothing but the job's name."""
    _run_periodic(
        state,
        _report_state_key(job["name"]),
        job,
        ["run-scheduled-report", "--job", job["name"]],
        now_utc,
    )


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
    """One scheduling pass: resolve the suppressed rest, then send the winners.

    Suppressed jobs are resolved for their *current* period only — after an
    outage they lose exactly the occurrence they lost the slot for, and come
    due again at their next natural boundary; nothing retro-sends.

    Suppression is persisted *before* any winner's subprocess runs, and each
    winner persists its own resolution as it succeeds (see ``_send_report``).
    One scheduling pass therefore never straddles a single unpersisted
    decision across a subprocess call: earlier revisions wrote suppressed
    resolutions only in a trailing ``write_state`` after every winner had run,
    so a crash during a later winner's (possibly slow) subprocess left the
    suppressed jobs' periods unresolved — still "due" — letting them send on
    the very next tick and blow past ``max_emails_per_day``.
    """
    due = _due_reports(state, now_utc, jobs)
    winners, suppressed = due[:max_emails_per_day], due[max_emails_per_day:]
    if suppressed:
        detail = (
            f"resolved without sending: suppressed by "
            f"max_emails_per_day={max_emails_per_day} "
            f"({', '.join(job['name'] for job in winners)} won)"
        )
        for job in suppressed:
            _resolve_without_sending(state, job, now_utc, detail)
        write_state(state)
    for job in winners:
        _send_report(state, job, now_utc)


def run_daemon() -> None:
    """The scheduler loop. Blocks forever; the service manager supervises."""
    schedule = load_schedule()
    jobs = load_jobs()
    logger.info(
        "penny daemon up: sync every {}h; balances daily at {}:00 NY; "
        "report jobs [{}], max {} email(s)/day",
        schedule["sync_interval_hours"],
        schedule["balances_hour"],
        ", ".join(job["name"] for job in jobs),
        schedule["max_emails_per_day"],
    )
    while True:
        state = read_state()
        now = datetime.now(UTC)
        if _due_sync(state, now, schedule["sync_interval_hours"]):
            _run_job(state, "sync", ["sync"])
        # Balances before reports, so a report sent this same tick can cite
        # the day's fresh sample.
        balances_job = _balances_job(schedule["balances_hour"])
        if _periodic_is_due(state, "balances", balances_job, now):
            _run_periodic(state, "balances", balances_job, ["capture-balances"], now)
        _tick_reports(state, now, jobs, schedule["max_emails_per_day"])
        time.sleep(_TICK_SECONDS)
