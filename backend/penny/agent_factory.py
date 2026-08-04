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
from agent_harness.core.models import Effort, ModelSettings, UsagePricer
from agent_harness.core.skills import SkillRegistry, build_skill_tool
from agent_harness.extras.reminders import ReminderQueue
from agent_harness.providers.anthropic import AnthropicMessagesModel, AnthropicProvider
from agent_harness.providers.google import GeminiModel, GoogleProvider
from agent_harness.providers.openai import OpenAIProvider, OpenAIResponsesModel
from agent_harness.providers.openrouter import (
    KIMI_K3,
    MOONSHOT_DIRECT,
    OpenRouterModel,
    OpenRouterProvider,
    RoutingPolicy,
)
from agent_harness.sandboxes.inprocess import InProcessSandbox
from agent_harness.sessions.inmemory import InMemorySession

from .config import agent_model
from .model_selection import parse_key, provider_family
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


AgentModel = (
    GeminiModel | OpenRouterModel | AnthropicMessagesModel | OpenAIResponsesModel
)
"""Every model shape ``build_model`` can return."""

# Per-model routing overrides for OpenRouter-served models. Kimi K3 has
# exactly one upstream endpoint (moonshotai/int4), so the harness's default
# US_FP8_ZDR policy matches zero endpoints and every request 404s — pinning
# MOONSHOT_DIRECT here means no caller has to know that. Absent means the
# harness default. NOT a list of known models: which models exist is the
# harness catalogue's business, and which are offered is ``model_selection``'s.
_OPENROUTER_ROUTING: dict[str, RoutingPolicy | None] = {
    KIMI_K3: MOONSHOT_DIRECT,
}


_ENV_KEY_BY_PROVIDER = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}


def _env_credential(
    credential: Credential | None, provider: str, model_name: str
) -> Credential:
    """The explicit credential, or the provider's platform key from the env.

    An explicit credential must match the provider family being built: with
    per-conversation models the family varies per request while a provisioned
    credential does not, and handing (say) a Google key to the Anthropic
    client would only surface as an opaque vendor 401 at request time. The
    missing-key error names both the variable and the model that needed it,
    so a misconfigured deploy fails with the fix in the message.
    """
    if credential is not None:
        if credential.provider != provider:
            raise RuntimeError(
                f"provisioned credential is for {credential.provider!r} but "
                f"model {model_name} needs a {provider!r} credential"
            )
        return credential
    env_var = _ENV_KEY_BY_PROVIDER[provider]
    api_key = os.environ.get(env_var, "").strip()
    if not api_key:
        raise RuntimeError(f"{env_var} is not set (required to run model {model_name})")
    return ApiKeyCredential(provider=provider, key=api_key)


def _build_openrouter_model(
    name: str, credential: Credential | None
) -> OpenRouterModel:
    """Build an OpenRouter-served model (Anthropic-compatible wire format).

    Capabilities resolve from the model id inside the harness, so Penny never
    restates context/output limits it would have to keep in sync.
    """
    return OpenRouterModel(
        provider=OpenRouterProvider(
            credential=_env_credential(credential, "openrouter", name)
        ),
        name=name,
        routing=_OPENROUTER_ROUTING.get(name),
    )


def build_model(
    *, credential: Credential | None = None, name: str | None = None
) -> AgentModel:
    """Build a FRESH model — one provider per request, never shared.

    The provider follows from the model *name*: OpenRouter ids are namespaced
    ``vendor/model`` (``moonshotai/kimi-k3``), so ``PENNY_AGENT_MODEL`` alone
    selects both the model and how to reach it — there is no second provider
    variable to keep consistent with it.

    The harness resolves a run's credential by mutating the provider client
    (``use_credential``); a provider shared across concurrent users would race
    (phase-2b decision D2). So every request builds its own provider here.

    Credentialing (the harness removed the ambient env-key fallback):
    - explicit ``credential`` — the per-user gate decision (BYO key or the
      platform subsidy key) — builds the provider client directly;
    - no ``credential`` — the dev/default path reads the platform key from
      the provider's env var (``GOOGLE_API_KEY`` / ``OPENROUTER_API_KEY``) and
      passes it as an explicit ``ApiKeyCredential`` so dev chat/cron still work.
    """
    # Always name the model explicitly (PENNY_AGENT_MODEL, or the caller's
    # override — e.g. the categorizer's PENNY_CATEGORIZER_MODEL) so the harness
    # default never silently applies. Composite selection keys
    # (``claude-opus-5:xhigh``) are accepted: the effort half belongs to
    # ``ModelSettings`` (see ``build_agent``), not to model construction.
    resolved, _ = parse_key(name or agent_model())
    # Dispatch on the catalogue's provider family — an unknown id raises in
    # provider_family rather than silently becoming a Gemini call.
    family = provider_family(resolved)
    if family == "openrouter":
        return _build_openrouter_model(resolved, credential)
    if family == "anthropic":
        return AnthropicMessagesModel(
            provider=AnthropicProvider(
                credential=_env_credential(credential, "anthropic", resolved)
            ),
            name=resolved,
        )
    if family == "openai":
        return OpenAIResponsesModel(
            provider=OpenAIProvider(
                credential=_env_credential(credential, "openai", resolved)
            ),
            name=resolved,
        )
    return GeminiModel(
        provider=GoogleProvider(
            credential=_env_credential(credential, "google", resolved)
        ),
        name=resolved,
    )


_DEFAULT_THINKING_BUDGET = 8192
"""``thinking_budget`` for every non-Gemini model.

Well under K3's 131k ``max_output_tokens`` — enough for real reasoning without
crowding out the answer.
"""


def _thinking_budget_for(model: AgentModel) -> int:
    """Thinking-token budget for the model — family-derived, not configurable.

    The budget is provider-generic in ``ModelSettings`` but its *domain* is
    not, so the value has to follow the model — which is why the old global
    ``PENNY_AGENT_THINKING_BUDGET`` override was removed: with per-conversation
    models, one process-wide number would hard-fail whichever family it was
    not written for.

    - Gemini reads ``-1`` as "dynamic" (model decides) and ``0`` as off. It
      must be non-None or the provider omits ``thinking_config`` and Gemini
      streams no thought summaries — the UI shows nothing between tool calls.
    - OpenRouter rides the Anthropic Messages shape, which forwards the number
      straight through as ``budget_tokens``; a negative value is rejected
      there, so ``-1`` would 400 every request.
    - Anthropic's adaptive-thinking models and OpenAI discard the number, but
      it must be non-None to gate the ``thinking`` block on (and with it the
      streamed thought summaries); depth is controlled by ``effort``.
    """
    return -1 if isinstance(model, GeminiModel) else _DEFAULT_THINKING_BUDGET


def build_agent(
    *,
    model: AgentModel,
    session: InMemorySession,
    effort: Effort | None = None,
    persist_session: bool = True,
    workspace_dir: Path | None = None,
    usage_pricer: UsagePricer | None = None,
    reminders: ReminderQueue | None = None,
    onboarding_resolver: OnboardingResolver | None = None,
) -> Agent:
    """Build the per-request Agent.

    ``workspace_dir`` overrides the agent's filesystem-sandbox root (e.g. the
    eval replays against a snapshot dir). Without it, the process-wide local
    workspace sandbox is used. ``effort`` is the conversation's pinned effort
    level (``None`` sends none — the vendor default applies); the thinking
    budget is family-derived, never configured.
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
        model_settings=ModelSettings(
            effort=effort, thinking_budget=_thinking_budget_for(model)
        ),
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
