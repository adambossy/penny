"""Email service for sending reports via Resend or SMTP."""

from __future__ import annotations

from dataclasses import dataclass
from email.message import EmailMessage
import os
import smtplib
import time
from typing import Any, cast

from loguru import logger
import resend


@dataclass(frozen=True, slots=True)
class SMTPConfig:
    """SMTP connection and authentication settings."""

    host: str
    port: int
    username: str
    password: str
    use_tls: bool = True
    use_ssl: bool = False


@dataclass
class EmailResult:
    """Result of an email send operation."""

    success: bool
    message_id: str | None
    error: str | None


class EmailService:
    """Email service using Resend API or SMTP."""

    def __init__(
        self,
        api_key: str | None = None,
        from_address: str = "reports@penny.com",
        from_name: str = "Penny Reports",
        *,
        provider: str = "resend",
        smtp_config: SMTPConfig | None = None,
    ) -> None:
        """Initialize email service.

        Args:
            api_key: Resend API key. If None, reads from RESEND_API_KEY env var.
            from_address: Email address to send from.
            from_name: Display name for sender.
            provider: Email provider, either ``resend`` or ``smtp``.
            smtp_config: SMTP configuration when provider is ``smtp``.
        """
        self._provider = provider.strip().lower()
        self._from_address = from_address
        self._from_name = from_name
        self._smtp_config = smtp_config

        if self._provider not in {"resend", "smtp"}:
            raise ValueError(
                f"Unsupported email provider: {provider}. Use 'resend' or 'smtp'."
            )

        if self._provider == "resend":
            self._api_key = api_key or os.environ.get("RESEND_API_KEY", "")
            if not self._api_key:
                raise ValueError(
                    "Resend API key required. "
                    "Set RESEND_API_KEY env var or pass api_key."
                )
            resend.api_key = self._api_key
        else:
            if smtp_config is None:
                raise ValueError("SMTP config required when provider is 'smtp'.")
            if not smtp_config.host.strip():
                raise ValueError("SMTP host is required when provider is 'smtp'.")
            if not smtp_config.username.strip():
                raise ValueError("SMTP username is required when provider is 'smtp'.")
            if not smtp_config.password.strip():
                raise ValueError("SMTP password is required when provider is 'smtp'.")

    def send_report(
        self,
        to: list[str],
        subject: str,
        html_content: str,
        text_content: str,
        max_retries: int = 3,
    ) -> EmailResult:
        """Send a report email.

        Args:
            to: List of recipient email addresses
            subject: Email subject
            html_content: HTML body content
            text_content: Plain text body content
            max_retries: Maximum retry attempts on failure

        Returns:
            EmailResult with success status and message ID
        """
        for attempt in range(max_retries):
            try:
                if self._provider == "smtp":
                    result = self._send_report_smtp(
                        to=to,
                        subject=subject,
                        html_content=html_content,
                        text_content=text_content,
                    )
                else:
                    result = self._send_report_resend(
                        to=to,
                        subject=subject,
                        html_content=html_content,
                        text_content=text_content,
                    )
                logger.bind(
                    provider=self._provider,
                    to=to,
                    message_id=result.message_id,
                    error=result.error,
                ).info(
                    "Report email {} via {} to {} (id={}, err={})",
                    "sent" if result.success else "FAILED",
                    self._provider,
                    ", ".join(to),
                    result.message_id,
                    result.error,
                )
                return result
            except Exception as e:
                if attempt < max_retries - 1:
                    # Exponential backoff: 1s, 2s, 4s
                    time.sleep(2**attempt)
                    continue

                logger.bind(provider=self._provider, to=to).error(
                    "Report email failed after {} attempts via {} to {}: {}",
                    max_retries,
                    self._provider,
                    ", ".join(to),
                    e,
                )
                return EmailResult(
                    success=False,
                    message_id=None,
                    error=f"Failed after {max_retries} attempts: {e}",
                )

        # Should not reach here, but satisfy type checker
        return EmailResult(
            success=False,
            message_id=None,
            error="Unexpected error in retry loop",
        )

    def _send_report_resend(
        self,
        *,
        to: list[str],
        subject: str,
        html_content: str,
        text_content: str,
    ) -> EmailResult:
        from_str = f"{self._from_name} <{self._from_address}>"
        params: dict[str, Any] = {
            "from": from_str,
            "to": to,
            "subject": subject,
            "html": html_content,
            "text": text_content,
        }

        response = resend.Emails.send(cast(Any, params))

        # Response is a dict with 'id' key on success
        if isinstance(response, dict) and "id" in response:
            return EmailResult(
                success=True,
                message_id=response["id"],
                error=None,
            )

        return EmailResult(
            success=False,
            message_id=None,
            error=f"Unexpected response: {response}",
        )

    def _send_report_smtp(
        self,
        *,
        to: list[str],
        subject: str,
        html_content: str,
        text_content: str,
    ) -> EmailResult:
        if self._smtp_config is None:
            raise ValueError("SMTP config required when provider is 'smtp'.")

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = f"{self._from_name} <{self._from_address}>"
        message["To"] = ", ".join(to)
        message.set_content(text_content)
        message.add_alternative(html_content, subtype="html")

        smtp_cfg = self._smtp_config
        if smtp_cfg.use_ssl:
            with smtplib.SMTP_SSL(smtp_cfg.host, smtp_cfg.port, timeout=30) as smtp:
                smtp.login(smtp_cfg.username, smtp_cfg.password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(smtp_cfg.host, smtp_cfg.port, timeout=30) as smtp:
                smtp.ehlo()
                if smtp_cfg.use_tls:
                    smtp.starttls()
                    smtp.ehlo()
                smtp.login(smtp_cfg.username, smtp_cfg.password)
                smtp.send_message(message)

        return EmailResult(success=True, message_id=None, error=None)

    def send_error_notification(
        self,
        to: list[str],
        error: str,
        job_metadata: dict[str, Any],
    ) -> EmailResult:
        """Send an error notification email.

        Args:
            to: List of recipient email addresses
            error: Error message
            job_metadata: Job metadata dict for debugging

        Returns:
            EmailResult with success status
        """
        subject = "Penny Report Failed"

        text_content = f"""Penny Report Generation Failed

Error: {error}

Job Metadata:
{self._format_metadata(job_metadata)}

Please check the Fly.io logs for more details.
"""

        html_content = f"""
<html>
<body>
<h1>Penny Report Generation Failed</h1>

<h2>Error</h2>
<pre>{error}</pre>

<h2>Job Metadata</h2>
<pre>{self._format_metadata(job_metadata)}</pre>

<p>Please check the Fly.io logs for more details.</p>
</body>
</html>
"""

        return self.send_report(
            to=to,
            subject=subject,
            html_content=html_content,
            text_content=text_content,
            max_retries=2,  # Fewer retries for error notifications
        )

    def _format_metadata(self, metadata: dict[str, Any]) -> str:
        """Format metadata dict as readable text."""
        lines = []
        for key, value in metadata.items():
            lines.append(f"  {key}: {value}")
        return "\n".join(lines)
