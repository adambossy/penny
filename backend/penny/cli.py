"""Headless Typer CLI — a peer front door beside ``penny.api.main``.

Drives the same agent the web bridge drives, minus the SSE translation: there
is no HTTP request, no chat UI, and no browser. A report's side effects
(email send) happen inside the agent's own tool calls exactly as in an
interactive run, so the CLI never re-implements report logic — it only
constructs the agent and runs it with the right prompt.

Segregation: this is *app code*, a front door (like ``api/main.py``), not
agent-internal. It may construct and drive the agent (``agent_factory``,
``bootstrap``, the services); it must never be imported by ``penny/tools`` or
the skills tree.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from dotenv import load_dotenv
from loguru import logger
import typer

from penny.observability import init_sentry

# Load env once at the entrypoint (project convention), without clobbering
# anything already injected into the environment.
load_dotenv(override=False)

# Error tracking as early as possible so CLI / scheduled-job crashes are
# reported. No-op when unconfigured.
init_sentry()

app = typer.Typer(
    help="Penny — personal-finance agent.",
    no_args_is_help=True,
)

_DEFAULT_MAX_TURNS = 50


def _render_template_vars(text: str) -> str:
    """Fill the per-run date placeholders a report prompt may carry.

    Mirrors the legacy CLI's default template vars (``CURRENT_DATE`` /
    ``CURRENT_MONTH`` / ``CURRENT_YEAR``) so a prompt that says "for the week
    ending {{CURRENT_DATE}}" resolves to today's date.
    """
    now = datetime.now(UTC)
    replacements = {
        "{{CURRENT_DATE}}": now.strftime("%Y-%m-%d"),
        "{{CURRENT_MONTH}}": now.strftime("%B"),
        "{{CURRENT_YEAR}}": now.strftime("%Y"),
    }
    for placeholder, value in replacements.items():
        text = text.replace(placeholder, value)
    return text


def _build_prompt(*, prompt: str | None, prompt_key: str | None) -> str:
    """Resolve the prompt text to drive the agent with.

    Exactly one of ``prompt`` / ``prompt_key`` is set. A ``prompt_key`` is
    loaded through the shared prompt loader and has its date placeholders
    filled. Recipients are **not** embedded in the prompt: ``send_email_report``
    reads them from the workspace config, so the prompt never names an address.
    """
    if prompt_key is not None:
        from penny.prompts import load_prompt

        return _render_template_vars(load_prompt(prompt_key))
    if prompt is not None:
        return prompt
    # callers guarantee exactly one is set; belt-and-suspenders
    raise ValueError("either prompt or prompt_key must be provided")


async def _drive_agent(*, prompt_text: str, max_turns: int) -> bool:
    """Construct the agent and run it once headlessly. Returns success.

    Uses the identical construction path the web bridge uses (``build_agent``
    with a fresh ``InMemorySession`` and ``persist_session=False``) so
    scheduled runs and chat run the same tools, skills, system prompt, and
    model. The agent's filesystem sandbox is the plain local workspace.
    """
    import contextlib

    from agent_harness.core.events import InMemoryEventBus
    from agent_harness.sessions.inmemory import InMemorySession

    from penny import observability
    from penny.agent_factory import build_agent, build_model

    session = InMemorySession(session_id=f"cli-{datetime.now(UTC):%Y%m%d%H%M%S}")
    # max_turns is accepted for parity with the legacy CLI surface; the harness
    # loop is currently bounded by the model producing a final output. Logged so
    # the value is visible in job logs.
    logger.bind(max_turns=max_turns).info("Driving headless agent run")

    # Only stand up an EventBus when Langfuse is on — chat uses one for the SSE
    # bridge, but headless runs have no other consumer.
    bus = InMemoryEventBus() if observability.is_enabled() else None
    trace_task = observability.start_run_trace_task(
        bus, source="cron", session_id=session.session_id, prompt=prompt_text
    )

    try:
        agent = build_agent(
            model=build_model(),
            session=session,
            persist_session=False,
        )
        result = await agent.run(prompt_text, event_bus=bus)
    finally:
        if bus is not None:
            await bus.close()
        if trace_task is not None:
            with contextlib.suppress(Exception):
                await trace_task
        # Short-lived process: flush buffered spans before the loop tears down.
        observability.flush()
    return result.output is not None


def _run_and_exit(*, prompt_text: str, max_turns: int) -> None:
    """Bootstrap, drive the agent, and map the outcome to an exit code."""
    from penny.bootstrap import bootstrap

    bootstrap()
    success = asyncio.run(_drive_agent(prompt_text=prompt_text, max_turns=max_turns))
    if not success:
        typer.echo("Agent run produced no final output", err=True)
        raise typer.Exit(1)
    typer.echo("Agent run completed")


@app.command("run-scheduled-report")
def run_scheduled_report(
    max_turns: int = typer.Option(
        _DEFAULT_MAX_TURNS, "--max-turns", help="Maximum agent turns."
    ),
) -> None:
    """Run today's scheduled report (New-York-time precedence).

    Drives the period-parameterized ``spending-report`` skill — there are no
    ``report-*`` prompt keys. Recipients come from the workspace config
    (``send_email_report`` needs no address).
    """
    from penny.services.scheduled_reports import (
        NEW_YORK_TZ,
        report_prompt,
        select_report_period,
    )

    now_utc = datetime.now(UTC)
    now_ny = now_utc.astimezone(NEW_YORK_TZ)
    period = select_report_period(now_utc=now_utc)
    typer.echo(
        f"Selected scheduled report period: {period} ({now_ny:%Y-%m-%d %H:%M:%S %Z})"
    )
    prompt_text = _build_prompt(prompt=report_prompt(period), prompt_key=None)
    _run_and_exit(prompt_text=prompt_text, max_turns=max_turns)


@app.command("run")
def run(
    prompt: str = typer.Option(
        None, "--prompt", help="Raw prompt text to send to the agent."
    ),
    prompt_key: str = typer.Option(
        None,
        "--prompt-key",
        help="Promptorium key to load (e.g. 'report-weekly-jenny').",
    ),
    max_turns: int = typer.Option(
        _DEFAULT_MAX_TURNS, "--max-turns", help="Maximum agent turns."
    ),
) -> None:
    """Run the agent on an explicit prompt or prompt key."""
    if prompt is None and prompt_key is None:
        typer.echo("Either --prompt or --prompt-key is required", err=True)
        raise typer.Exit(1)
    if prompt is not None and prompt_key is not None:
        typer.echo("Only one of --prompt or --prompt-key may be provided", err=True)
        raise typer.Exit(1)

    prompt_text = _build_prompt(prompt=prompt, prompt_key=prompt_key)
    _run_and_exit(prompt_text=prompt_text, max_turns=max_turns)


@app.command("sync")
def sync(
    count: int = typer.Option(250, "--count", help="Max transactions per Plaid page."),
) -> None:
    """Sync + categorize the latest transactions from every connected item.

    The headless peer of the ``sync_transactions`` agent tool, run on a
    schedule by ``penny daemon``.
    """
    from penny.adapters.clients.plaid import PlaidClient
    from penny.bootstrap import bootstrap
    from penny.db import get_db
    import penny.observability as observability
    from penny.services import build_categorizer, get_taxonomy
    from penny.tools._services.sync_service import SyncTool

    bootstrap()

    async def _sync() -> dict[str, object]:
        sync_tool = SyncTool(
            plaid_client=PlaidClient.from_env(),
            categorizer_factory=build_categorizer,
            db=get_db(),
            taxonomy=get_taxonomy(),
        )
        summary = await sync_tool.sync(count=count)
        return summary.to_dict()

    try:
        r = asyncio.run(_sync())
    finally:
        # Flush so per-transaction categorizer traces export before we exit.
        observability.flush()

    typer.echo(
        f"Sync complete: added={r.get('total_added')} "
        f"modified={r.get('total_modified')} removed={r.get('total_removed')}"
    )
    relink = r.get("relink_required_items") or []
    # A stale bank connection is a user-action item, not a job failure — the
    # sync still ran (other items + categorization). Report it; don't exit
    # non-zero.
    if relink:
        typer.echo(
            f"Connections needing re-authentication: {', '.join(sorted(relink))}"
        )


@app.command("eval-categorizer")
def eval_categorizer(
    limit: int = typer.Option(
        None,
        "--limit",
        min=1,
        help="Sample the most recent N (for testing); a limited run does not "
        "advance the watermark.",
    ),
    email: list[str] = typer.Option(
        None, "--email", help="Recipient(s) for the per-run status email (repeatable)."
    ),
) -> None:
    """Run one categorizer eval.

    Snapshots finance data into a local writable SQLite copy, replays the new
    agent on the copy, records durable eval rows, and emails a status line
    (with the report when legacy and agent disagree). Right/wrong is read later
    from your corrections — there is no staging step.
    """
    from penny.eval.job import run_eval
    import penny.observability as observability

    try:
        result = asyncio.run(run_eval(limit=limit, email_to=email or None))
    except Exception as exc:
        typer.echo(f"Eval failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    finally:
        # Force-flush spans so per-turn traces export before this short-lived
        # process exits (no-op when Langfuse is disabled).
        observability.flush()
    typer.echo(f"Eval {result.get('status')}: {result}")


@app.command("migrate")
def migrate_cmd() -> None:
    """Apply alembic migrations to head (Postgres only; SQLite uses bootstrap).

    Idempotent; non-zero exit on failure.
    """
    from penny.schema import upgrade_to_head

    upgrade_to_head()  # env.py reads DATABASE_URL
    logger.info("penny migrate: schema at head")


def main() -> None:
    """Console-script entry point (``[project.scripts] penny``)."""
    app()


if __name__ == "__main__":
    main()
