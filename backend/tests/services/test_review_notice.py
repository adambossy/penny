"""The daily review notice — the pipeline's heartbeat.

It is sent whether or not anything is pending: that is what makes silence
mean "sync or the daemon stopped" rather than "nothing to do today".
"""

from __future__ import annotations

import pytest

from penny.services.review_notice import review_notice_content


def test_notice_names_the_backlog_and_links_the_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PENNY_BASE_URL", raising=False)
    subject, text, html = review_notice_content(pending=7, reviewed=42)

    assert "7 transaction(s) to review" == subject
    assert "http://localhost:8000/review" in text
    assert "http://localhost:8000/review" in html
    assert "42" in text


def test_notice_is_still_sent_when_the_queue_is_empty() -> None:
    subject, text, _ = review_notice_content(pending=0, reviewed=42)

    assert subject == "Nothing to review"
    assert "labeled" in text


def test_base_url_override_makes_the_link_work_off_box(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mail is read on a phone; the link must point at the reachable host."""
    monkeypatch.setenv("PENNY_BASE_URL", "http://penny.tail1234.ts.net:8000/")
    _, text, _ = review_notice_content(pending=1, reviewed=0)

    assert "http://penny.tail1234.ts.net:8000/review" in text
