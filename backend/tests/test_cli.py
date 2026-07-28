"""Unit tests for the headless Typer CLI front door.

Covers the scheduled-report selection precedence (pure date logic) and a
smoke test that ``_run_and_exit`` constructs the agent through the real
``build_agent`` seam and maps the run outcome to an exit code — with the
model, email, and network fully stubbed (no live run).
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
import typer

from penny import cli
from penny.services.scheduled_reports import report_prompt, select_report_period


def _at(iso: str) -> datetime:
    """Build an aware UTC datetime from an ISO string."""
    return datetime.fromisoformat(iso).replace(tzinfo=UTC)


@pytest.mark.parametrize(
    "now_iso,expected_period",
    [
        # 2026-01-01 12:00 UTC is still Jan 1 in New York -> annual.
        ("2026-01-01T12:00:00", "annual"),
        # 2026-03-01 12:00 UTC is the 1st in NY -> monthly.
        ("2026-03-01T12:00:00", "monthly"),
        # 2026-06-14 is a Sunday -> weekly.
        ("2026-06-14T12:00:00", "weekly"),
        # 2026-06-11 is a Thursday -> daily.
        ("2026-06-11T12:00:00", "daily"),
        # Jan 1 wins over the day-1 / weekday checks (precedence).
        ("2026-01-01T12:00:00", "annual"),
    ],
)
def test_select_report_period_precedence(now_iso: str, expected_period: str) -> None:
    # input
    now_utc = _at(now_iso)

    # act
    output = select_report_period(now_utc=now_utc)

    # expected
    expected_output = expected_period

    # assert
    assert output == expected_output


def test_select_report_period_ny_day_boundary_uses_local_calendar() -> None:
    # input: 2026-01-01 02:00 UTC is still 2025-12-31 21:00 in New York,
    # so the annual rule must NOT fire (it is not yet Jan 1 locally).
    now_utc = _at("2026-01-01T02:00:00")

    # act
    output = select_report_period(now_utc=now_utc)

    # expected: Dec 31 2025 is a Wednesday -> daily.
    expected_output = "daily"

    # assert
    assert output == expected_output


@pytest.mark.parametrize("period", ["daily", "weekly", "monthly", "annual"])
def test_report_prompt_triggers_skill_for_period(period: str) -> None:
    # act
    output = report_prompt(period)

    # expected: a natural-language request that names the period and the
    # spending-report skill (so the agent loads the skill, no prompt key), and
    # asks for email delivery (the skill only sends when the request asks).
    expected_output = (
        f"Generate my {period} spending report for the current period and "
        "email it to me."
    )

    # assert
    assert output == expected_output


def test_build_prompt_does_not_embed_recipients() -> None:
    # Recipients come from PENNY_REPORT_RECIPIENTS via send_email_report,
    # never embedded in the prompt — so the prompt passes through untouched.
    output = cli._build_prompt(prompt="Summarize spending.", prompt_key=None)

    # assert
    assert output == "Summarize spending."


def test_build_prompt_requires_a_source() -> None:
    # act / assert: belt-and-suspenders guard raises
    with pytest.raises(ValueError):
        cli._build_prompt(prompt=None, prompt_key=None)


def _patch_run_and_exit_seams(
    monkeypatch: pytest.MonkeyPatch, *, output: Any
) -> dict[str, Any]:
    """Stub bootstrap + agent construction so no live run happens.

    Returns a dict capturing what prompt the stubbed agent was driven with,
    so the smoke test can assert the CLI reached the real ``build_agent``
    seam with the expected prompt text.
    """
    captured: dict[str, Any] = {}

    class _StubAgent:
        async def run(
            self, prompt_text: str, *, event_bus: Any = None
        ) -> SimpleNamespace:
            captured["prompt"] = prompt_text
            return SimpleNamespace(output=output)

    def _fake_build_agent(**kwargs: Any) -> _StubAgent:
        captured["built"] = True
        return _StubAgent()

    # build_agent / build_model are imported lazily inside _drive_agent from
    # penny.agent_factory, so patch them on that module.
    import penny.agent_factory as factory

    monkeypatch.setattr(factory, "build_model", lambda: object())
    monkeypatch.setattr(factory, "build_agent", _fake_build_agent)

    # bootstrap is imported lazily inside _run_and_exit from penny.bootstrap.
    import penny.bootstrap as bootstrap_mod

    monkeypatch.setattr(
        bootstrap_mod, "bootstrap", lambda: captured.__setitem__("booted", True)
    )
    return captured


def test_run_and_exit_success_drives_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # input: a prompt and a stubbed agent that returns a final output
    captured = _patch_run_and_exit_seams(monkeypatch, output="done")

    # act: should not raise (exit 0)
    cli._run_and_exit(prompt_text="hello", max_turns=3)

    # expected: bootstrap ran, the agent was built and driven with the prompt
    expected_output = {"booted": True, "built": True, "prompt": "hello"}

    # assert
    assert captured == expected_output


def test_run_and_exit_no_output_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # input: a stubbed agent that yields no final output
    _patch_run_and_exit_seams(monkeypatch, output=None)

    # act / assert: maps to a non-zero exit
    with pytest.raises(typer.Exit) as exc_info:
        cli._run_and_exit(prompt_text="hello", max_turns=3)

    # expected
    expected_code = 1

    # assert
    assert exc_info.value.exit_code == expected_code
