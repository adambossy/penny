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
import os
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger
import typer

from penny.observability import init_sentry
from penny.settings import apply_config_to_env

# Load env once at the entrypoint (project convention), without clobbering
# anything already injected into the environment; then the workspace
# config.toml supplies defaults for anything still unset.
_had_real_workspace_env = "PENNY_WORKSPACE" in os.environ
load_dotenv(override=False)

# backend/.env is symlinked into every worktree by the WorktreeCreate hook
# (single source of truth for secrets), so a PENNY_WORKSPACE set there is
# identical everywhere too — every worktree ends up sharing one workspace.
# .penny-workspace is a per-worktree, gitignored, un-symlinked (the hook
# only links .env / .env.* / *.env) escape hatch: drop a
# PENNY_WORKSPACE=<path> line in it to give one worktree its own
# memory/reports/config.toml without touching the shared secrets file.
# Only allowed to beat .env's own PENNY_WORKSPACE, never a real environment
# variable set before this module was imported (e.g. a test harness's
# redirect) — that must always win, per the project's env-contract rule.
if not _had_real_workspace_env:
    load_dotenv(
        Path(__file__).resolve().parent.parent / ".penny-workspace", override=True
    )
apply_config_to_env()

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
    from agent_harness.extras.reminders import InMemoryReminderQueue
    from agent_harness.sessions.inmemory import InMemorySession

    from penny import observability
    from penny.agent_factory import (
        announce_skill_manifest,
        build_agent,
        build_model,
        load_skill_registry,
    )

    session = InMemorySession(session_id=f"cli-{datetime.now(UTC):%Y%m%d%H%M%S}")
    # A scheduled/headless run gets no app-provisioned reminder queue (that's
    # website-only, see api/routes.py) — a plain in-memory one is enough to
    # carry the skill manifest into this run's first turn.
    reminders = InMemoryReminderQueue()
    skill_registry = load_skill_registry()
    await announce_skill_manifest(reminders, session.session_id, skill_registry)
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
            reminders=reminders,
            skill_registry=skill_registry,
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
    job: str = typer.Option(
        ...,
        "--job",
        help="Name of the [[jobs]] entry in config.toml to run.",
    ),
    max_turns: int = typer.Option(
        _DEFAULT_MAX_TURNS, "--max-turns", help="Maximum agent turns."
    ),
) -> None:
    """Run one configured report job (the daemon invokes this per due job).

    Drives the period-parameterized ``spending-report`` skill — there are no
    ``report-*`` prompt keys. The job's own ``recipients`` (when set) are
    exported as ``PENNY_REPORT_RECIPIENTS`` here, so a manual run behaves
    exactly like a daemon-spawned one; otherwise the ambient default applies
    (``send_email_report`` needs no address either way).
    """
    import os

    from penny.services.scheduled_reports import report_prompt
    from penny.settings import load_jobs

    jobs = {entry["name"]: entry for entry in load_jobs()}
    job_config = jobs.get(job)
    if job_config is None:
        typer.echo(
            f"No report job named {job!r} in config.toml [[jobs]] "
            f"(configured: {', '.join(sorted(jobs)) or 'none'})",
            err=True,
        )
        raise typer.Exit(1)

    if job_config["recipients"]:
        os.environ["PENNY_REPORT_RECIPIENTS"] = ",".join(job_config["recipients"])
    typer.echo(f"Running report job {job!r} (period={job_config['period']})")
    prompt_text = _build_prompt(prompt=report_prompt(job_config), prompt_key=None)
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
    from penny.bootstrap import bootstrap
    import penny.observability as observability
    from penny.tools._services.sync_service import SyncTool

    bootstrap()

    async def _sync() -> dict[str, object]:
        summary = await SyncTool.from_env().sync(count=count)
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


@app.command("capture-balances")
def capture_balances() -> None:
    """Capture every linked account's current balances as daily samples.

    Appends one ``account_balances`` row per account and (self-healingly)
    registers any account missing from ``plaid_accounts``. Run daily by
    ``penny daemon``. A connection needing re-authentication is a reported
    user-action line, not a failure — mirroring ``penny sync``.
    """
    from penny.adapters.clients.plaid import PlaidClient
    from penny.bootstrap import bootstrap
    from penny.db import get_db
    from penny.tools._services.balance_capture import capture_account_balances

    bootstrap()
    summary = capture_account_balances(get_db(), PlaidClient.from_env())

    typer.echo(
        f"Balance capture complete: accounts={summary.accounts_captured} "
        f"newly_registered={summary.accounts_registered} "
        f"items={summary.items_captured} failed={summary.items_failed}"
    )
    if summary.relink_required_items:
        typer.echo(
            "Connections needing re-authentication: "
            f"{', '.join(sorted(summary.relink_required_items))}"
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


@app.command("init")
def init() -> None:
    """Interactive onboarding: workspace, database, keys, email, daemon.

    Every step is re-runnable; answers land in the workspace ``config.toml``
    (chmod 600) which every entrypoint loads as environment defaults. Real
    environment variables always win.
    """
    from pathlib import Path

    from penny.identity import local_user_id
    from penny.settings import (
        SCHEDULE_DEFAULTS,
        load_config,
        load_jobs,
        write_config,
    )
    from penny.workspace import resolve_workspace_dir

    existing = load_config()
    prior_env: dict[str, str] = {
        k: str(v) for k, v in (existing.get("env") or {}).items()
    }
    prior_schedule = {**SCHEDULE_DEFAULTS, **(existing.get("schedule") or {})}

    def ask(key: str, prompt: str, *, default: str = "", secret: bool = False) -> None:
        """Prompt for one env value, defaulting to the stored answer."""
        current = prior_env.get(key, default)
        shown = "(set)" if secret and current else current
        value = typer.prompt(prompt, default=shown or "", show_default=bool(shown))
        if secret and value == "(set)":
            return  # keep the stored secret
        if value:
            prior_env[key] = value  # empty re-entry keeps the prior answer

    # 1. Workspace ("data room").
    workspace = resolve_workspace_dir()
    typer.echo(f"Workspace: {workspace}")
    for sub in ("memory", "reports", "logs"):
        (workspace / sub).mkdir(parents=True, exist_ok=True)
    local_user_id()  # mint the stable user ref on first run
    if workspace.name == ".transactoid":
        typer.echo(
            "  (using your existing ~/.transactoid; move it to ~/.penny anytime "
            "and both names keep working)"
        )

    # 2. Database.
    default_db = prior_env.get(
        "PENNY_DATABASE_URL", f"sqlite:///{workspace / 'penny.db'}"
    )
    ask(
        "PENNY_DATABASE_URL",
        "Database URL (SQLite path or postgres:// URL)",
        default=default_db,
    )

    # 3. Model provider.
    ask("GOOGLE_API_KEY", "Google (Gemini) API key", secret=True)
    ask("PENNY_AGENT_MODEL", "Agent model", default="gemini-3.6-flash")

    # 4. Plaid. The secret is stored under the env-specific var the client
    # actually reads (PLAID_<ENV>_SECRET), so ask for the environment first.
    ask("PLAID_CLIENT_ID", "Plaid client id")
    while True:
        ask(
            "PLAID_ENV",
            "Plaid environment (sandbox/development/production)",
            default="production",
        )
        plaid_env = (prior_env.get("PLAID_ENV") or "production").strip().lower()
        if plaid_env in ("sandbox", "development", "production"):
            prior_env["PLAID_ENV"] = plaid_env
            break
        typer.echo(f"  {plaid_env!r} is not a Plaid environment — try again.")
        prior_env.pop("PLAID_ENV", None)
    ask(f"PLAID_{plaid_env.upper()}_SECRET", "Plaid secret", secret=True)

    # 5. Email (direct SMTP; a local MTA works via SMTP_HOST=localhost).
    ask("SMTP_HOST", "SMTP host", default="smtp.gmail.com")
    ask("SMTP_PORT", "SMTP port", default="587")
    ask("SMTP_USERNAME", "SMTP username (usually your email)")
    ask("SMTP_PASSWORD", "SMTP password (e.g. a Gmail app password)", secret=True)
    ask(
        "PENNY_REPORT_RECIPIENTS",
        "Report recipient email(s), comma-separated",
        default=prior_env.get("SMTP_USERNAME", ""),
    )

    # Unprompted schedule keys (email cap, balances hour) carry over from the
    # stored config so a re-run of init never resets a hand-edit.
    schedule = {
        **prior_schedule,
        "sync_interval_hours": int(
            typer.prompt(
                "Sync every N hours",
                default=str(prior_schedule["sync_interval_hours"]),
            )
        ),
    }
    # The wizard configures one weekly report job (YAGNI: more jobs are a
    # hand-edit, not a UI). A config that already carries [[jobs]] is kept
    # verbatim so re-running init never clobbers a hand-tuned schedule.
    jobs = load_jobs()  # the stored [[jobs]], or the single weekly default
    if not existing.get("jobs"):
        weekly = jobs[0]
        weekly["weekday"] = int(
            typer.prompt(
                "Weekly report weekday (1=Mon … 7=Sun)",
                default=str(weekly["weekday"]),
            )
        )
        weekly["hour"] = int(
            typer.prompt(
                "Weekly report hour (local, 0-23)", default=str(weekly["hour"])
            )
        )
    path = write_config(prior_env, schedule, jobs)
    typer.echo(f"Wrote {path}")
    typer.echo(
        "  Additional report jobs (cadence, recipients, priority) are "
        "configured by editing [[jobs]] in that file."
    )

    # Apply immediately so the steps below see the answers.
    apply_config_to_env()

    # 6. Database setup: migrate (postgres) or create (sqlite), then seed.
    from penny.bootstrap import prepare_database

    prepare_database()
    typer.echo("Database ready (schema + taxonomy).")

    # 7. Daemon.
    if typer.confirm(
        "Install + start the background daemon (sync + reports)?", default=True
    ):
        from penny.service_install import install_and_start

        typer.echo(install_and_start(Path(workspace) / "logs"))

    typer.echo(
        "\nDone. Next steps:\n"
        "  penny serve         # local web UI at http://127.0.0.1:8000\n"
        "  penny daemon status # scheduler health\n"
        "Claude Code plugin (same tools, same database):\n"
        "  /plugin marketplace add adambossy/penny\n"
        "  /plugin install penny@penny\n"
        "Remote access from your phone: use Tailscale (recommended) — an ngrok\n"
        "URL is a public door to your finances; protect it with ngrok's auth."
    )


@app.command("mcp")
def mcp_cmd() -> None:
    """Serve Penny's toolsets over stdio MCP (for Claude Code and friends).

    The harness owns the conversation loop; this process only exposes the
    tools plus fresh runtime context (date, schema, taxonomy, memory) via the
    MCP initialize instructions. Blocks until the client disconnects.
    """
    from penny.mcp_server import run_stdio

    run_stdio()


_daemon_app = typer.Typer(help="The background scheduler (sync + reports).")
app.add_typer(_daemon_app, name="daemon")


@_daemon_app.command("run")
def daemon_run() -> None:
    """Run the scheduler loop in the foreground (what the service invokes)."""
    from penny.daemon import run_daemon

    run_daemon()


# "install" and "start" are the same idempotent operation ((re)write the
# service definition, (re)start it) — one body, two spellings.
@_daemon_app.command("install")
@_daemon_app.command("start")
def daemon_start() -> None:
    """Install (if needed) + start the daemon as a user service."""
    from penny.service_install import install_and_start
    from penny.workspace import resolve_logs_dir

    typer.echo(install_and_start(resolve_logs_dir()))


@_daemon_app.command("stop")
def daemon_stop() -> None:
    """Stop the daemon service."""
    from penny.service_install import stop

    typer.echo(stop())


@_daemon_app.command("status")
def daemon_status() -> None:
    """Show whether the daemon runs and each job's last outcome."""
    import json as _json

    from penny.daemon_state import read_state, state_path
    from penny.service_install import is_running

    typer.echo(f"service running: {is_running()}")
    state = read_state()
    if not state:
        typer.echo(f"no job state yet ({state_path()})")
        return
    typer.echo(_json.dumps(state, indent=2))


def _frontend_build_stamp(dist: Path) -> dict | None:
    """This app's build stamp (``penny-build.json``), or ``None`` if absent/corrupt/foreign.

    ``npm run build`` writes the stamp (``vite.config.ts``); a dist without a
    matching one is a stale pre-split or foreign build — the incident this
    guards against served a gitignored pre-split dist, Clerk landing page and
    all, against the no-auth backend.
    """
    import json

    try:
        stamp = json.loads((dist / "penny-build.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(stamp, dict) or stamp.get("app") != "penny-single-player":
        return None
    return stamp


def _frontend_dist_ok(dist: Path) -> bool:
    """True when ``dist`` carries this app's build stamp."""
    return _frontend_build_stamp(dist) is not None


# Inputs that determine what a build actually produces. `node_modules`
# itself is deliberately excluded — any dependency version that matters is
# already reflected in `package.json` / `package-lock.json`, so hashing the
# lockfiles is enough to catch an `npm install` drift without walking every
# installed package.
_FRONTEND_SOURCE_DIRS = ("src", "packages")
_FRONTEND_SOURCE_FILES = ("package.json", "package-lock.json", "vite.config.ts")
_FRONTEND_SOURCE_EXCLUDE_DIRS = {"node_modules", "dist", ".git"}


def _frontend_newest_source_mtime(frontend_dir: Path) -> float:
    """The newest mtime among files that feed the build.

    Compared against the dist's stamped ``builtAt`` to tell a build that
    predates a later source or dependency change — the mtime approach costs
    a stat-tree walk, not a hash, and a `git pull` or `npm install` already
    bumps mtimes on every file it touches, so it catches exactly the drift
    that matters (uncommitted edits included).
    """
    newest = 0.0
    for name in _FRONTEND_SOURCE_FILES:
        path = frontend_dir / name
        if path.exists():
            newest = max(newest, path.stat().st_mtime)
    for dirname in _FRONTEND_SOURCE_DIRS:
        root = frontend_dir / dirname
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if _FRONTEND_SOURCE_EXCLUDE_DIRS & set(path.relative_to(root).parts):
                continue
            newest = max(newest, path.stat().st_mtime)
    return newest


def _frontend_dist_stale(dist: Path, frontend_dir: Path) -> bool:
    """True when ``dist`` is missing, foreign/unstamped, or older than its sources."""
    stamp = _frontend_build_stamp(dist)
    if stamp is None:
        return True
    from datetime import datetime as _datetime

    try:
        built_at = _datetime.fromisoformat(str(stamp["builtAt"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return True
    return _frontend_newest_source_mtime(frontend_dir) > built_at.timestamp()


def _rebuild_frontend(frontend_dir: Path) -> bool:
    """Run ``npm install && npm run build``; True on success.

    Output streams straight to the console (no capture) — a build failure's
    detail belongs in the terminal, not swallowed into an exception message.
    """
    import shutil
    import subprocess

    npm = shutil.which("npm")
    if npm is None:
        typer.echo("npm not found on PATH — cannot rebuild the frontend.", err=True)
        return False
    for args in (["install"], ["run", "build"]):
        result = subprocess.run([npm, *args], cwd=frontend_dir)  # noqa: S603 - npm found via PATH, args are our own literals
        if result.returncode != 0:
            typer.echo(f"`npm {' '.join(args)}` failed — see output above.", err=True)
            return False
    return True


def _resolve_frontend_dist(
    explicit_dir: str | None, *, repo_root: Path | None = None
) -> Path | None:
    """The dist to serve, rebuilding the repo's default one when it's stale.

    An explicit ``--frontend-dir`` is the caller's own choice: it must exist
    and carry the stamp, but is never rebuilt (there is no source tree this
    command can assume for an arbitrary path) — a bad one is a hard error,
    not a silent downgrade. The repo-managed default (``frontend/dist``) is
    instead kept fresh automatically: missing, foreign, or older than its own
    sources triggers ``npm install && npm run build`` before serving.

    ``repo_root`` defaults to this checkout's root; overridable for tests.
    """
    if explicit_dir is not None:
        static = Path(explicit_dir).expanduser()
        if not static.exists():
            typer.echo(f"Frontend dir not found: {static}", err=True)
            raise typer.Exit(1)
        if not _frontend_dist_ok(static):
            typer.echo(
                f"{static} has no penny-build.json stamp — it was not built "
                "by this app's `npm run build` (a stale pre-split or foreign "
                "build). Rebuild it, or pass a freshly built dist.",
                err=True,
            )
            raise typer.Exit(1)
        return static

    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent.parent
    frontend_dir = repo_root / "frontend"
    candidate = frontend_dir / "dist"
    if not frontend_dir.exists():
        # No source tree to build from at all — the repo-managed default
        # can't be a stamped dist without it (`candidate` lives under it).
        typer.echo(
            "No built frontend found — serving the API only. "
            "Build it with `npm run build` in frontend/."
        )
        return None

    if not candidate.exists() or _frontend_dist_stale(candidate, frontend_dir):
        typer.echo(
            "Frontend is missing or stale — rebuilding "
            "(`npm install && npm run build` in frontend/)…"
        )
        if not _rebuild_frontend(frontend_dir):
            typer.echo(
                "Rebuild failed — serving the API only. Fix the error above "
                "and re-run `penny serve`, or build manually.",
                err=True,
            )
            return None

    if candidate.exists() and _frontend_dist_ok(candidate):
        return candidate
    typer.echo(
        "No built frontend found — serving the API only. "
        "Build it with `npm run build` in frontend/."
    )
    return None


@app.command("serve")
def serve(
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Bind address. Keep the localhost default; reach it from other "
        "devices via Tailscale (recommended) rather than binding 0.0.0.0.",
    ),
    port: int = typer.Option(8000, "--port", help="Port to serve on."),
    frontend_dir: str = typer.Option(
        None,
        "--frontend-dir",
        help="Directory of the built web UI (defaults to the repo's "
        "frontend/dist when present).",
    ),
    all_in_one: bool = typer.Option(
        False,
        "--all-in-one",
        help="Also run the scheduler (sync + reports) inside this process — "
        "for casual use without the installed daemon service.",
    ),
) -> None:
    """Run the Penny web app locally (API + built web UI, one process).

    On a Postgres database the alembic chain is applied first (idempotent);
    SQLite builds its schema at startup. The web UI is served from the built
    frontend — the repo's own ``frontend/dist`` is kept fresh automatically
    (rebuilt when missing or stale); without one at all, the API alone runs
    (or use the Vite dev server against it for development).
    """
    import uvicorn

    from penny.api.app import AppConfig, create_app
    from penny.bootstrap import prepare_database

    prepare_database()

    static = _resolve_frontend_dist(frontend_dir)

    if all_in_one:
        import threading

        from penny.daemon import run_daemon

        threading.Thread(target=run_daemon, daemon=True, name="penny-daemon").start()
        typer.echo("Scheduler co-running in-process (--all-in-one).")

    application = create_app(AppConfig(static_dir=static))
    typer.echo(f"Penny running at http://{host}:{port}")
    uvicorn.run(application, host=host, port=port, log_level="info")


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
