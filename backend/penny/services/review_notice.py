"""The daily "transactions waiting to be reviewed" email.

One email per day, sent whether or not anything is waiting. That is the point:
the message doubles as the pipeline's heartbeat, so silence means sync or the
daemon stopped — a signal a "only when there's work" email could never give.

It replaces the categorizer eval's old status email, which reported agreement
between the sync-time categorizer and a replay of that same categorizer and so
always read 100%.
"""

from __future__ import annotations

import os

from loguru import logger

from penny.services.email import build_email_service, resolve_report_recipients


def _review_url() -> str:
    """Where the review page lives for this install.

    ``PENNY_BASE_URL`` lets a non-default host/port (or a Tailscale name)
    produce a link that works from the phone the mail is read on.
    """
    base = os.environ.get("PENNY_BASE_URL", "http://localhost:8000").rstrip("/")
    return f"{base}/review"


def review_notice_content(pending: int, reviewed: int) -> tuple[str, str, str]:
    """Subject, text and HTML for the daily notice."""
    url = _review_url()
    if pending:
        subject = f"{pending} transaction(s) to review"
        lead = (
            f"{pending} transaction(s) are waiting for a category label. "
            "Confirming the categorizer's pick is one keystroke each."
        )
    else:
        subject = "Nothing to review"
        lead = "Every synced transaction has been labeled."
    tail = (
        f"{reviewed} transaction(s) labeled so far — the ground truth the "
        "categorizer is scored against."
    )
    text = f"{lead}\n\n{url}\n\n{tail}\n"
    html = f'<p>{lead}</p><p><a href="{url}">Open the review page</a></p><p>{tail}</p>'
    return subject, text, html


def send_review_notice() -> dict[str, object]:
    """Send today's notice. Returns a summary; raises if delivery fails.

    A raise is deliberate: the daemon only spends a job's period on success,
    so a failed send is retried on the next tick rather than silently
    swallowing the day's heartbeat.
    """
    from penny.db import get_db

    db = get_db()
    pending = db.pending_review_count()
    reviewed = db.review_scoreboard()["reviewed"]
    subject, text, html = review_notice_content(pending, reviewed)

    recipients = resolve_report_recipients()
    result = build_email_service().send_report(
        to=recipients, subject=subject, html_content=html, text_content=text
    )
    if not result.success:
        raise RuntimeError(f"review notice not delivered: {result.error}")
    logger.info(
        "review notice sent to {}: {} pending, {} labeled",
        ", ".join(recipients),
        pending,
        reviewed,
    )
    return {"pending": pending, "reviewed": reviewed, "recipients": recipients}
