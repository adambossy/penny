"""The eval job: one unattended run (every 12h).

Cohort (ingested since the last watermark) -> writable SQLite snapshot of prod
finance data (through the read-only role, RLS-scoped) -> replay the agent on the
snapshot -> record durable eval rows to prod -> upload the run's artifacts
(report HTML, SQLite fixture, items JSON) to R2 -> email a status line every run
(a link to the hosted report when legacy!=agent disagree). Nothing waits on a
human; right/wrong is read later from your corrections.

No Neon branch, no ``neonctl``, no control-plane credential: the replay runs on
a local SQLite copy, so the untrusted agent can never reach prod. Bulk finance
reads go through the SELECT-only role (``PENNY_AGENT_READONLY_DATABASE_URL``);
only the narrow eval-store writes use the read-write ``DATABASE_URL``.

The eval deliberately reuses the agent's read-only role rather than minting a
dedicated one: both are SELECT-only and RLS-scoped, so a second credential earns
nothing. Split to a ``PENNY_EVAL_READONLY_DATABASE_URL`` only if the eval's read
grants ever need to diverge from the agent's.
"""

from __future__ import annotations

import contextlib
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from loguru import logger
from sqlalchemy import select

from penny.adapters.db.facade import DB
from penny.adapters.db.models import Category, DerivedTransaction, PlaidTransaction
from penny.adapters.storage.r2 import public_url_for_key, store_object_in_r2
from penny.eval.fixture import build_fixture_bytes, snapshot_to_sqlite
from penny.eval.replay import replay_one
from penny.eval.report import disagreements, render_eval_report
from penny.eval.version import version_stamp


def _send_status_email(
    to: list[str],
    run_at: datetime,
    *,
    status: str,
    n_items: int = 0,
    n_disagree: int = 0,
    report_url: str | None = None,
    error: BaseException | None = None,
) -> bool:
    """Send the one-per-run status email (heartbeat / report link / failure alert).

    Exactly one email goes out per run so that *no* email means the pipeline
    itself stopped — a signal a disagreement-only email could never give.
    """
    from penny.tools.delivery import _build_email_service

    service = _build_email_service()
    when = f"{run_at:%Y-%m-%d %H:%M}"
    if status == "failed":
        subject = f"Categorizer eval {when} — FAILED"
        text = f"The categorizer eval failed: {error}\n"
        html = f"<p>The categorizer eval <b>failed</b>: {error}</p>"
    elif status == "skipped_empty":
        subject = f"Categorizer eval {when} — nothing new"
        text = "No new transactions to evaluate.\n"
        html = "<p>No new transactions to evaluate.</p>"
    else:  # completed
        subject = f"Categorizer eval {when} — {n_disagree}/{n_items} disagreements"
        text = (
            f"Categorizer eval for {when}: {n_items} transactions, {n_disagree} "
            "legacy-vs-agent disagreements.\n"
        )
        html = (
            f"<p>Categorizer eval for {when}: {n_items} transactions, "
            f"{n_disagree} legacy-vs-agent disagreements.</p>"
        )
        if report_url:
            text += f"\nReport: {report_url}\n"
            html += f'<p><a href="{report_url}">Open the report</a></p>'
    result = service.send_report(
        to=to, subject=subject, html_content=html, text_content=text
    )
    return bool(getattr(result, "success", False))


async def run_eval(
    *,
    limit: int | None = None,
    email_to: list[str] | None = None,
    cohort_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Run one eval.

    ``limit`` samples the most recent N (for testing); a limited run does not
    advance the watermark. Must be a positive integer.
    ``cohort_ids`` overrides cohort selection with an explicit set (a targeted
    rerun / backtest); in that mode the ingest watermark is left untouched so a
    one-off run can't skip future daily cohorts.
    """
    if limit is not None and limit <= 0:
        raise ValueError(f"--limit must be a positive integer, got {limit}")
    prod_url = os.environ.get("DATABASE_URL", "").strip()
    if not prod_url:
        raise RuntimeError("DATABASE_URL is required for the eval job")
    prod = DB(prod_url)  # read-write handle: eval-store reads + writes only
    if prod.dialect == "sqlite":
        prod.create_schema()  # dev/test: build eval_* (+ finance) from models
    # Postgres: eval_runs/eval_items are alembic-owned (migration 007) and
    # already present; create_all is refused on a durable schema.
    run_at = datetime.now()
    # Carries the cohort size + household as soon as they are known, so a failure
    # after cohort selection records a diagnostic size (not a misleading 0) and
    # stamps the household on the failed row.
    progress: dict[str, Any] = {"cohort_size": 0}

    try:
        result = await _run_eval_core(
            prod,
            run_at,
            limit=limit,
            cohort_ids=cohort_ids,
            progress=progress,
        )
    except Exception as exc:  # noqa: BLE001 - re-raised after alerting
        # Durable failure record (best-effort). cohort_max_created_at stays NULL,
        # so the watermark does NOT advance and the cohort is retried next run.
        with contextlib.suppress(Exception):
            prod.record_eval_run(
                run_at=run_at,
                status="failed",
                cohort_size=progress["cohort_size"],
            )
        logger.exception("eval: run failed")
        if email_to:
            _emit_status_email(email_to, run_at, status="failed", error=exc)
        raise

    # Heartbeat: one email on EVERY completed/skipped run so a silent pipeline
    # (no email at all) is distinguishable from a healthy one.
    if email_to:
        _emit_status_email(
            email_to,
            run_at,
            status=result["status"],
            n_items=result.get("cohort_size", 0),
            n_disagree=result.get("disagreements", 0),
            report_url=result.get("report_url"),
        )
    return result


def _emit_status_email(to: list[str], run_at: datetime, **kwargs: Any) -> None:
    """Send the per-run status email; log (never raise) if it does not go out.

    A soft failure (provider returns success=False without raising) would
    otherwise look identical to a healthy silent run, defeating the heartbeat —
    so a non-delivery is logged, not swallowed.
    """
    try:
        sent = _send_status_email(to, run_at, **kwargs)
    except Exception as exc:  # noqa: BLE001 - email is non-critical
        logger.warning("eval: status email raised (run unaffected): {}", exc)
        return
    if not sent:
        logger.warning("eval: status email not delivered (provider returned failure)")


async def _run_eval_core(
    prod: DB,
    run_at: datetime,
    *,
    limit: int | None,
    cohort_ids: list[int] | None,
    progress: dict[str, Any],
) -> dict[str, Any]:
    """Do the eval; return the result dict. Emailing is the caller's job."""
    import penny.db as pdb

    explicit_cohort = cohort_ids is not None
    readonly = pdb.get_readonly_db()
    since = None if explicit_cohort else prod.last_eval_watermark()
    is_limited = limit is not None and not explicit_cohort
    if explicit_cohort:
        cohort_ids = list(cohort_ids or [])
    else:
        cohort_ids = prod.derived_ids_created_since(since)
        if is_limited:
            # Most-recent N. A limited run is a non-committing sample (watermark
            # not advanced below), so it never strands the rows it dropped.
            cohort_ids = cohort_ids[-limit:]
    if not cohort_ids:
        prod.record_eval_run(
            run_at=run_at,
            status="skipped_empty",
            cohort_size=0,
        )
        logger.info("eval: empty cohort; skipped")
        return {"status": "skipped_empty", "cohort_size": 0, "disagreements": 0}
    progress["cohort_size"] = len(cohort_ids)

    # A targeted rerun (explicit cohort) or a capped sample (--limit) must not
    # advance the daily watermark — only a full daily cohort does.
    watermark = (
        None
        if (explicit_cohort or is_limited)
        else prod.max_created_at_for_ids(cohort_ids)
    )
    run_label = f"eval-{run_at:%Y%m%dT%H%M%S}"

    # Build a writable SQLite snapshot of the finance closure through the
    # read-only handle.
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = snapshot_to_sqlite(readonly, Path(tmp) / "fixture.sqlite")

        with snapshot.session() as session:
            rows = session.execute(
                select(
                    DerivedTransaction.transaction_id,
                    DerivedTransaction.merchant_descriptor,
                    DerivedTransaction.amount_cents,
                    DerivedTransaction.posted_at,
                    Category.key,
                    PlaidTransaction.raw_name,
                )
                .select_from(DerivedTransaction)
                .outerjoin(
                    Category, Category.category_id == DerivedTransaction.category_id
                )
                .outerjoin(
                    PlaidTransaction,
                    PlaidTransaction.plaid_transaction_id
                    == DerivedTransaction.plaid_transaction_id,
                )
                .where(DerivedTransaction.transaction_id.in_(cohort_ids))
            ).all()
        base = {
            r[0]: {
                "merchant_descriptor": r[1],
                "amount": (r[2] / 100.0) if r[2] is not None else None,
                "date": r[3].isoformat() if r[3] else None,
                "legacy_key": r[4],
                "raw_name": r[5],
            }
            for r in rows
        }

        # Completeness guard: the RLS-scoped snapshot must contain every cohort
        # row. A gap means a household member is absent from PENNY_CRON_USER_IDS
        # (their private rows are invisible to every principal). Fail loudly — the
        # watermark does not advance, so the cohort is retried once the config is
        # fixed — rather than silently dropping those transactions.
        missing = [tid for tid in cohort_ids if tid not in base]
        if missing:
            raise RuntimeError(
                f"snapshot missing {len(missing)}/{len(cohort_ids)} cohort rows — a "
                "household member is likely absent from PENNY_CRON_USER_IDS; refusing "
                "to advance the watermark past unevaluated transactions"
            )
        logger.info("eval: cohort={} -> snapshot {}", len(cohort_ids), run_label)

        # Replay the agent on the snapshot. Point BOTH the read-write and the
        # read-only global handles at the local copy, so no agent tool (now or a
        # future one backed by get_readonly_db) can reach live prod during the
        # untrusted replay.
        items: list[dict[str, Any]] = []
        saved_db, saved_ro = pdb._db, pdb._readonly_db
        pdb._db = pdb._readonly_db = snapshot
        try:
            for tid in cohort_ids:
                meta = base.get(tid, {})
                txn = {
                    "transaction_id": tid,
                    "merchant_descriptor": meta.get("merchant_descriptor"),
                    "raw_name": meta.get("raw_name"),
                    "amount": meta.get("amount"),
                    "date": meta.get("date"),
                }
                decision = await replay_one(txn)
                items.append({**txn, "legacy_key": meta.get("legacy_key"), **decision})
        finally:
            pdb._db, pdb._readonly_db = saved_db, saved_ro

        # Render BEFORE recording 'completed' so a render bug fails the run (the
        # watermark does not advance) instead of contradicting an already-committed
        # completed row.
        version = version_stamp()
        disagree = disagreements(items)
        html_doc = render_eval_report(
            items, run_at=run_at.isoformat(timespec="seconds"), version=version
        )

        # Atomic: the completed run + its items commit together (the watermark-
        # advancing write) as the LAST fallible DB op — nothing after can crash and
        # write a contradictory 'failed' row.
        run_id = prod.record_eval_run(
            run_at=run_at,
            status="completed",
            cohort_size=len(cohort_ids),
            cohort_max_created_at=watermark,
            branch_name=run_label,
            version=version,
            items=items,
        )

        # Upload the run's artifacts to R2 under one prefix: the report HTML, the
        # SQLite fixture (for backtests), and the machine-readable items JSON.
        # The fixture + items hold real transaction data, so they go in the
        # private default bucket; only the report is uploaded to the (optional)
        # PUBLIC report bucket so its emailed link can be permanent. Best-effort:
        # a failed upload must not lose the run. r2_fixture_url stores the fixture
        # KEY (backtests download by key); report_url is a shareable link.
        prefix = f"eval-runs/{run_label}"
        fixture_key = f"{prefix}/fixture.tar.gz"
        report_key = f"{prefix}/report.html"
        report_bucket = os.environ.get("R2_REPORT_BUCKET", "").strip() or None
        r2_meta = {"eval_run_id": str(run_id), "run_label": run_label}
        report_url: str | None = None
        try:
            store_object_in_r2(
                key=fixture_key,
                body=build_fixture_bytes(snapshot),
                content_type="application/gzip",
                metadata=r2_meta,
            )
            prod.set_eval_run_fixture(run_id, fixture_key)
            store_object_in_r2(
                key=f"{prefix}/items.json",
                body=json.dumps(items, default=str, indent=2).encode("utf-8"),
                content_type="application/json",
                metadata=r2_meta,
            )
            store_object_in_r2(
                key=report_key,
                body=html_doc.encode("utf-8"),
                content_type="text/html; charset=utf-8",
                metadata=r2_meta,
                bucket=report_bucket,
            )
            report_url = public_url_for_key(report_key, bucket=report_bucket)
        except Exception as exc:  # noqa: BLE001 - artifacts are non-critical
            logger.warning("eval: R2 artifact upload failed (run kept): {}", exc)

        logger.info(
            "eval: completed run {} ({} items, {} disagreements)",
            run_id,
            len(items),
            len(disagree),
        )
        return {
            "status": "completed",
            "eval_run_id": run_id,
            "cohort_size": len(cohort_ids),
            "disagreements": len(disagree),
            "run_label": run_label,
            "fixture_key": fixture_key,
            "report_url": report_url,
        }
