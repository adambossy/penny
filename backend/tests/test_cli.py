"""Unit tests for the headless Typer CLI front door.

Covers ``run-scheduled-report --job`` resolution against the configured
``[[jobs]]`` (including per-job recipients export) and a smoke test that
``_run_and_exit`` constructs the agent through the real ``build_agent`` seam
and maps the run outcome to an exit code — with the model, email, and network
fully stubbed (no live run).
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import pytest
import typer

from penny import cli

_JOBS = [
    {"name": "daily", "period": "daily", "hour": 8, "recipients": None, "priority": 1},
    {
        "name": "weekly",
        "period": "weekly",
        "weekday": 1,
        "hour": 8,
        "recipients": ["me@example.com", "spouse@example.com"],
        "priority": 2,
    },
]

_AMBIENT_RECIPIENTS = "ambient@example.com"


def _patch_jobs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub the configured jobs and capture what the command runs with.

    ``load_jobs`` is imported lazily inside the command from ``penny.settings``
    and the run itself goes through ``_run_and_exit``; patch both seams. An
    ambient PENNY_REPORT_RECIPIENTS is set (and restored by monkeypatch) so
    the per-job override is observable.
    """
    import penny.settings as settings

    captured: dict[str, Any] = {}
    monkeypatch.setenv("PENNY_REPORT_RECIPIENTS", _AMBIENT_RECIPIENTS)
    monkeypatch.setattr(settings, "load_jobs", lambda: [dict(j) for j in _JOBS])
    monkeypatch.setattr(
        cli,
        "_run_and_exit",
        lambda *, prompt_text, max_turns: captured.update(
            prompt=prompt_text, max_turns=max_turns
        ),
    )
    return captured


def test_run_scheduled_report_picks_the_named_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # input
    captured = _patch_jobs(monkeypatch)

    # act
    cli.run_scheduled_report(job="weekly", max_turns=3)

    # expected: the weekly job's period + window drive the prompt, which
    # still explicitly asks for email delivery (commit 1696ebf) and states
    # this is unattended so the agent resolves stale data itself instead of
    # asking a question no one will answer (an observed run asked "shall I
    # sync now?" and silently never sent) — and the job's own recipients
    # override the ambient default for the send tool.
    expected_prompt = (
        "Generate my weekly spending report covering this week and email it "
        "to me. This is an unattended scheduled run — there is no one "
        "available to answer a question, so do not ask for confirmation or "
        "wait for a reply. If the current period has no data yet, sync "
        "first; if data is still stale afterward, report on the most recent "
        "available period instead and note the staleness. Always finish by "
        "generating and emailing a report — never end by asking a question."
    )

    # assert
    assert captured == {"prompt": expected_prompt, "max_turns": 3}
    assert os.environ["PENNY_REPORT_RECIPIENTS"] == (
        "me@example.com,spouse@example.com"
    )


def test_run_scheduled_report_without_recipients_keeps_ambient_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # input: the daily job carries no recipients of its own
    captured = _patch_jobs(monkeypatch)

    # act
    cli.run_scheduled_report(job="daily", max_turns=3)

    # assert: the run happened and the ambient recipients were left alone
    assert captured["max_turns"] == 3
    assert os.environ["PENNY_REPORT_RECIPIENTS"] == _AMBIENT_RECIPIENTS


def test_run_scheduled_report_unknown_job_fails_clearly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # input
    captured = _patch_jobs(monkeypatch)

    # act / assert: unknown name exits non-zero without driving the agent
    with pytest.raises(typer.Exit) as exc_info:
        cli.run_scheduled_report(job="fortnightly", max_turns=3)

    assert exc_info.value.exit_code == 1
    assert captured == {}


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
