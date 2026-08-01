"""Construct the agent-harness ``Agent`` the backend serves.

One shared ``Agent`` factory; a fresh ``Agent`` is built per request so each
conversation carries its own session. Tools come from four sources:

- :func:`build_toolset` — Penny's core domain tools.
- :func:`build_amazon_toolset` — Amazon plugin (self-contained subpackage).
- :class:`FilesystemTools` — read/write/edit/grep/glob/list_dir on the
  workspace sandbox.
- :func:`build_skill_tool` — progressive-disclosure skill registry.
"""

from __future__ import annotations

from datetime import date, timedelta
import os
from pathlib import Path

from agent_harness import Agent
from agent_harness.core.credentials import ApiKeyCredential, Credential
from agent_harness.core.filesystem import FilesystemTools
from agent_harness.core.models import ModelSettings, UsagePricer
from agent_harness.core.skills import SkillRegistry, build_skill_tool
from agent_harness.extras.reminders import ReminderQueue
from agent_harness.providers.google import GeminiModel, GoogleProvider
from agent_harness.sandboxes.inprocess import InProcessSandbox
from agent_harness.sessions.inmemory import InMemorySession

from .config import agent_model
from .plugins.amazon import build_amazon_toolset
from .prompts import load_prompt
from .sandbox import get_sandbox
from .tools._services.onboarding import OnboardingResolver
from .tools.registry import build_toolset


def _sql_dialect_from_env() -> str:
    """Return ``postgresql`` or ``sqlite`` from the configured database URL."""
    from .db import resolve_database_url

    url = resolve_database_url().lower()
    if url.startswith(("postgresql://", "postgresql+", "postgres+")):
        return "postgresql"
    return "sqlite"


_CORE_MEMORY_FILES = ("index.md", "merchant-rules.md")


def _assemble_agent_memory(workspace_dir: Path | None = None) -> str:
    """Concatenate the core memory files from the workspace.

    Reads ``index.md`` then ``merchant-rules.md`` (joined with blank lines);
    empty string if neither exists. With ``workspace_dir`` (the per-run hybrid
    checkout, phase 1b) it reads ``<workspace_dir>/memory``; without it, the
    legacy ``~/.transactoid/memory`` — kept so scripts/tests with no checkout
    still resolve memory.
    """
    from .workspace import resolve_memory_dir

    memory_dir = (
        workspace_dir / "memory" if workspace_dir is not None else resolve_memory_dir()
    )
    if not memory_dir.exists() or not memory_dir.is_dir():
        return ""
    parts: list[str] = []
    for name in _CORE_MEMORY_FILES:
        path = memory_dir / name
        if path.exists():
            parts.append(path.read_text(encoding="utf-8"))
    return "\n\n".join(parts)


def render_system_prompt(workspace_dir: Path | None = None) -> str:
    """Render the penny-system-prompt prompt with full runtime context.

    Fills: today's date + ISO week, DB dialect + dialect directives, schema
    snapshot, taxonomy snapshot, and agent memory. (taxonomy-rules is NOT
    injected here — that 35 KB block belongs to the categorizer prompt;
    main never put it in the agent loop either.) ``workspace_dir`` scopes
    ``{{AGENT_MEMORY}}`` to the per-run hybrid checkout when present.
    """
    import yaml  # local import — keeps top-level import cost light

    from .db import get_db
    from .services import get_taxonomy

    today = date.today()
    week_start = today - timedelta(days=today.isoweekday() - 1)
    week_end = week_start + timedelta(days=6)
    dialect = _sql_dialect_from_env()

    # Heavy blocks — produced fresh per request so taxonomy edits / schema
    # migrations during the session are reflected immediately.
    try:
        schema_yaml = yaml.dump(
            get_db().compact_schema_hint(), default_flow_style=False, sort_keys=False
        )
    except Exception:
        schema_yaml = "(schema unavailable)"
    try:
        taxonomy_yaml = yaml.dump(
            get_taxonomy().to_prompt(), default_flow_style=False, sort_keys=False
        )
    except Exception:
        taxonomy_yaml = "(taxonomy unavailable)"
    try:
        sql_directives = load_prompt(f"sql-directives-{dialect}")
    except Exception:
        sql_directives = ""

    replacements = {
        "{{CURRENT_DATE}}": today.isoformat(),
        "{{CURRENT_WEEKDAY}}": today.strftime("%A"),
        "{{WEEK_START}}": week_start.isoformat(),
        "{{WEEK_END}}": week_end.isoformat(),
        "{{SQL_DIALECT}}": dialect,
        "{{SQL_DIALECT_DIRECTIVES}}": sql_directives,
        "{{DATABASE_SCHEMA}}": schema_yaml,
        "{{CATEGORY_TAXONOMY}}": taxonomy_yaml,
        # Phase 1b: memory comes from the per-run hybrid checkout when the
        # front door materialized one; else the legacy workspace dir.
        "{{AGENT_MEMORY}}": _assemble_agent_memory(workspace_dir)
        or "(no memory files yet)",
    }
    rendered = load_prompt("penny-system-prompt")
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    return rendered


_PROJECT_ROOT = Path(__file__).resolve().parent.parent  # .../backend/


def build_model(
    *, credential: Credential | None = None, name: str | None = None
) -> GeminiModel:
    """Build a FRESH Gemini model — one provider per request, never shared.

    The harness resolves a run's credential by mutating the provider client
    (``use_credential``); a provider shared across concurrent users would race
    (phase-2b decision D2). So every request builds its own provider here.

    Credentialing (the harness removed the ambient env-key fallback):
    - explicit ``credential`` — the per-user gate decision (BYO key or the
      platform subsidy key) — builds the provider client directly;
    - no ``credential`` — the dev/default path reads the platform key from
      ``GOOGLE_API_KEY`` and passes it as an explicit ``ApiKeyCredential`` so
      dev chat/cron still work.
    """
    if credential is None:
        api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("GOOGLE_API_KEY is not set")
        credential = ApiKeyCredential(provider="google", key=api_key)
    # Always name the model explicitly (PENNY_AGENT_MODEL, or the caller's
    # override — e.g. the categorizer's PENNY_CATEGORIZER_MODEL) so the harness
    # default never silently applies.
    return GeminiModel(
        provider=GoogleProvider(credential=credential), name=name or agent_model()
    )


def _thinking_budget_from_env() -> int:
    """Thinking-token budget passed to the model (``PENNY_AGENT_THINKING_BUDGET``).

    Gemini semantics: ``-1`` = dynamic (model decides how much to think),
    ``0`` would disable thinking entirely. Must be set to a non-None value or
    the provider omits ``thinking_config`` and Gemini streams no thought
    summaries — the UI then shows nothing between tool calls while the model
    thinks.
    """
    raw = os.environ.get("PENNY_AGENT_THINKING_BUDGET", "").strip()
    return int(raw) if raw else -1


def build_agent(
    *,
    model: GeminiModel,
    session: InMemorySession,
    persist_session: bool = True,
    workspace_dir: Path | None = None,
    usage_pricer: UsagePricer | None = None,
    reminders: ReminderQueue | None = None,
    onboarding_resolver: OnboardingResolver | None = None,
) -> Agent:
    """Build the per-request Agent.

    ``workspace_dir`` overrides the agent's filesystem-sandbox root (e.g. the
    eval replays against a snapshot dir). Without it, the process-wide local
    workspace sandbox is used.
    """
    sandbox = (
        InProcessSandbox(root=str(workspace_dir))
        if workspace_dir is not None
        else get_sandbox()
    )

    skill_registry = SkillRegistry.load(project_root=_PROJECT_ROOT, user_root=None)
    skill_tool = build_skill_tool(skill_registry)

    from agent_harness import StaticToolset

    skills_toolset = StaticToolset(name="skills", tools=[skill_tool])
    filesystem_tools = FilesystemTools(sandbox=sandbox)

    return Agent(
        name="penny",
        model=model,
        instructions=render_system_prompt(workspace_dir),
        session=session,
        persist_session=persist_session,
        sandbox=sandbox,
        model_settings=ModelSettings(thinking_budget=_thinking_budget_from_env()),
        # A subsidized run carries a pricer so the loop emits ModelUsage events
        # the billing subscriber accrues; a BYO run passes None (no metering).
        usage_pricer=usage_pricer,
        # Injected by the website (phase 5): a DB-backed ReminderQueue whose
        # pending reminders the run loop drains into the next user message. The
        # factory only sees the harness Protocol — the web-backed implementation
        # is constructed by the caller, keeping agent/website segregation intact.
        reminders=reminders,
        toolsets=[
            # The onboarding-resolve op is website-owned persistence; the website
            # constructs it and passes it here (like reminders/usage_pricer) so
            # the factory only sees the OnboardingResolver Protocol.
            build_toolset(onboarding_resolver=onboarding_resolver),
            build_amazon_toolset(),
            filesystem_tools,
            skills_toolset,
        ],
    )
