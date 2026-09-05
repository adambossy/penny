"""Delivery tools — upload reports to R2 and email them.

These wrap ``penny.tools._services.uploader.upload_artifact`` and
``penny.services.email.EmailService`` so the agent (or the
``render-report-html`` skill) can hand off a rendered report end-to-end.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from agent_harness import tool

from penny.services.email import build_email_service, resolve_report_recipients
from penny.tools._services.uploader import upload_artifact


@tool
async def upload_artifact_to_r2(
    artifact_type: str,
    body: str,
    content_type: str,
) -> dict[str, Any]:
    """Upload an artifact (report HTML / markdown / etc.) to Cloudflare R2.

    R2 credentials are read from env (``R2_ENDPOINT_URL``, ``R2_BUCKET``,
    ``R2_ACCESS_KEY_ID``, ``R2_SECRET_ACCESS_KEY``).

    Args:
        artifact_type: Logical type, e.g. ``report-html``, ``report-md``.
        body: UTF-8 body to upload.
        content_type: MIME type, e.g. ``text/html; charset=utf-8``.

    Returns:
        ``{"status": "success", "key": ..., "bucket": ..., "content_type": ...}``
        on success; ``{"status": "error", "message": ...}`` on failure.
    """

    def _run() -> dict[str, Any]:
        try:
            stored = upload_artifact(
                artifact_type=artifact_type,
                body=body.encode("utf-8"),
                content_type=content_type,
                timestamp=datetime.now(UTC),
            )
            return {
                "status": "success",
                "key": stored.key,
                "bucket": stored.bucket,
                "content_type": stored.content_type,
            }
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    return await asyncio.to_thread(_run)


@tool
async def send_email_report(
    subject: str,
    html_content: str,
    text_content: str,
) -> dict[str, Any]:
    """Email a rendered report to the configured recipient(s).

    Recipients come from configuration (``PENNY_REPORT_RECIPIENTS``, set at
    onboarding) and cannot be named or influenced by the caller. There is
    deliberately no recipient argument.

    Args:
        subject: Email subject line.
        html_content: HTML body.
        text_content: Plain-text fallback body.
    """

    def _run() -> dict[str, Any]:
        try:
            recipients = resolve_report_recipients()
            service = build_email_service()
            result = service.send_report(
                to=recipients,
                subject=subject,
                html_content=html_content,
                text_content=text_content,
            )
            return {
                "status": "success" if result.success else "error",
                "message_id": result.message_id,
                "error": result.error,
            }
        except Exception as exc:
            return {"status": "error", "message_id": None, "error": str(exc)}

    return await asyncio.to_thread(_run)
