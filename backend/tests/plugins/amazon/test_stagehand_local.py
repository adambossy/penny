"""Tests for the Stagehand LOCAL backend's pure helpers and the auth phase.

The browser-driving parts need a real Chrome, so what is pinned here is the
logic that decides *where* a session lives, *whether* Amazon bounced us to
the sign-in portal, and *which* backends need an up-front auth phase at all.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from penny.plugins.amazon.backends.order_history import final_url, is_signed_out
from penny.plugins.amazon.backends.stagehand_local import profile_data_dir
from penny.plugins.amazon.scraper import _ensure_auth


class _Response:
    """Minimal stand-in for the SDK's navigate response object."""

    def __init__(self, result: Any) -> None:
        self.data = type("Data", (), {"result": result})()


def test_final_url_prefers_the_pages_current_url() -> None:
    response = _Response(
        {
            "page": {"_currentUrl": "https://www.amazon.com/your-orders/orders"},
            "response": {"url": "https://www.amazon.com/redirected"},
        }
    )
    assert final_url(response) == "https://www.amazon.com/your-orders/orders"


def test_final_url_falls_back_to_the_http_response_url() -> None:
    response = _Response({"response": {"url": "https://www.amazon.com/ap/signin"}})
    assert final_url(response) == "https://www.amazon.com/ap/signin"


def test_final_url_is_none_when_navigation_returned_no_response() -> None:
    assert final_url(_Response(None)) is None
    assert final_url(_Response({"page": {}})) is None


def test_is_signed_out_recognizes_amazons_auth_portal() -> None:
    assert is_signed_out("https://www.amazon.com/ap/signin?openid.mode=checkid_setup")
    assert is_signed_out("https://www.amazon.com/ap/challenge?arb=1")
    assert not is_signed_out("https://www.amazon.com/your-orders/orders")
    assert not is_signed_out(None)


def test_profile_data_dir_is_per_profile_and_filesystem_safe() -> None:
    adam = profile_data_dir("adambossy@gmail.com")
    jenny = profile_data_dir("jloleary0@gmail.com")

    assert adam != jenny
    assert adam.name == "adambossy_gmail.com"
    assert adam.parent == jenny.parent
    assert profile_data_dir(None).name == "default"


class _Profile:
    """Stand-in for AmazonLoginProfileDB."""

    def __init__(self, *, context_id: str | None) -> None:
        self.profile_key = "adambossy@gmail.com"
        self.display_name = "Adam"
        self.profile_id = 1
        self.browserbase_context_id = context_id
        self.history_complete_through: date | None = None


class _ExplodingDB:
    """Any DB call during a pass-through auth phase is a bug."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"_ensure_auth must not call db.{name}()")


def test_ensure_auth_skips_the_login_phase_for_local_backends() -> None:
    # Why: the login phase used to run for every backend as a scrape with
    # max_orders=0 — falsy, so the cap never fired and the local backend paid
    # for a full, discarded scrape before the real one.
    profile = _Profile(context_id=None)

    assert _ensure_auth(_ExplodingDB(), profile, "stagehand") is profile


def test_ensure_auth_passes_through_an_existing_browserbase_context() -> None:
    profile = _Profile(context_id="ctx-123")

    assert _ensure_auth(_ExplodingDB(), profile, "stagehand-browserbase") is profile
