from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Literal

Provider = Literal["openai", "claude", "gemini", "langgraph"]
ReasoningEffort = Literal["low", "medium", "high"]
Verbosity = Literal["low", "medium", "high"]
Environment = Literal["production", "development"]


def penny_env() -> Environment:
    """The environment tier this process runs in (``PENNY_ENV``).

    Fail closed: unset means ``production`` (dev-only conveniences off), so a
    forgotten variable costs a developer a convenience, never a user a
    protection. Local dev opts in with ``PENNY_ENV=development`` in
    ``backend/.env``; deploy also pins ``production`` explicitly
    (``deploy/env/deploy.env.template``). Gate dev-only behavior on this
    single var rather than minting per-feature flags.
    """
    value = os.environ.get("PENNY_ENV", "").strip().lower() or "production"
    if value not in ("production", "development"):
        raise ValueError(
            f"PENNY_ENV must be 'production' or 'development', got {value!r}"
        )
    return value  # type: ignore[return-value]


# The Gemini model Penny runs on when no env override is set. Single source of
# truth for the default — every module that needs a model name resolves it
# through agent_model()/categorizer_model() below rather than pinning its own.
DEFAULT_AGENT_MODEL = "gemini-3.6-flash"


def agent_model() -> str:
    """The chat agent's model name (``PENNY_AGENT_MODEL``).

    Falls back to :data:`DEFAULT_AGENT_MODEL`; never raises, so best-effort
    consumers (eval stamps, metadata) can call it safely.
    """
    return os.environ.get("PENNY_AGENT_MODEL", "").strip() or DEFAULT_AGENT_MODEL


def categorizer_model() -> str:
    """The categorizer's model name (``PENNY_CATEGORIZER_MODEL``).

    Falls back to the agent model, so setting only ``PENNY_AGENT_MODEL``
    moves both; the categorizer var exists to diverge them deliberately.
    """
    return os.environ.get("PENNY_CATEGORIZER_MODEL", "").strip() or agent_model()


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """Runtime provider configuration loaded at process startup."""

    provider: Provider
    model: str
    reasoning_effort: ReasoningEffort = "medium"
    verbosity: Verbosity = "high"
    enable_web_search: bool = True
    memory_dir: Path = Path.home() / ".penny" / "memory"
    reports_dir: Path = Path.home() / ".penny" / "reports"
    skills_project_dir: str = ".claude/skills"
    skills_user_dir: str = "~/.claude/skills"
    skills_builtin_dir: str = "src/penny/skills"


def _require_langgraph_credentials(model: str) -> None:
    """Validate API credentials for the LangGraph provider based on model prefix."""
    if model.startswith("anthropic:"):
        _require_env("ANTHROPIC_API_KEY")
    elif model.startswith("openai:"):
        _require_env("OPENAI_API_KEY")
    else:
        # Default to Google credentials for gemini or unknown model names
        _require_env("GOOGLE_API_KEY")


def _normalize_langgraph_model(model: str) -> str:
    """Normalize model to LangGraph's provider-prefixed format."""
    normalized = model.strip()
    if ":" in normalized:
        return normalized

    lowered = normalized.lower()
    if lowered.startswith(("anthropic/", "claude")):
        base_model = normalized.split("/", 1)[1] if "/" in normalized else normalized
        return f"anthropic:{base_model}"
    if lowered.startswith(("openai/", "gpt", "o")):
        base_model = normalized.split("/", 1)[1] if "/" in normalized else normalized
        return f"openai:{base_model}"
    if lowered.startswith(("vertexai/",)):
        base_model = normalized.split("/", 1)[1]
        return f"vertexai:{base_model}"
    if lowered.startswith(("google_genai/", "google/")):
        base_model = normalized.split("/", 1)[1]
        return f"google_genai:{base_model}"
    return f"google_genai:{normalized}"


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def load_runtime_config_from_env() -> RuntimeConfig:
    """Load runtime config from env and validate startup requirements."""
    from penny.workspace import resolve_memory_dir, resolve_reports_dir

    memory_dir = resolve_memory_dir()
    reports_dir = resolve_reports_dir()
    provider_value = os.environ.get("PENNY_AGENT_PROVIDER", "openai").strip()
    if provider_value not in {"openai", "claude", "gemini", "langgraph"}:
        raise ValueError(
            "PENNY_AGENT_PROVIDER must be one of: openai, claude, gemini, langgraph"
        )
    provider: Provider = provider_value  # type: ignore[assignment]

    model_default_map = {
        "openai": "gpt-5.5",
        "claude": "",
        "gemini": DEFAULT_AGENT_MODEL,
        "langgraph": "gemini-3-flash-preview",
    }
    default_model = model_default_map[provider]
    model = os.environ.get("PENNY_AGENT_MODEL", default_model).strip()
    if not model:
        raise ValueError("PENNY_AGENT_MODEL is required when provider is claude")

    reasoning_effort = os.environ.get("PENNY_AGENT_REASONING_EFFORT", "medium").strip()
    if reasoning_effort not in {"low", "medium", "high"}:
        raise ValueError(
            "PENNY_AGENT_REASONING_EFFORT must be one of: low, medium, high"
        )

    verbosity = os.environ.get("PENNY_AGENT_VERBOSITY", "high").strip()
    if verbosity not in {"low", "medium", "high"}:
        raise ValueError("PENNY_AGENT_VERBOSITY must be one of: low, medium, high")

    enable_web_search = os.environ.get(
        "PENNY_ENABLE_WEB_SEARCH", "true"
    ).strip().lower() in {"1", "true", "yes", "on"}

    skills_project_dir = os.environ.get(
        "PENNY_AGENT_SKILLS_PROJECT_DIR", ".claude/skills"
    ).strip()
    skills_user_dir = os.environ.get(
        "PENNY_AGENT_SKILLS_USER_DIR", "~/.claude/skills"
    ).strip()
    skills_builtin_dir = os.environ.get(
        "PENNY_AGENT_SKILLS_BUILTIN_DIR", "src/penny/skills"
    ).strip()

    if provider == "langgraph":
        model = _normalize_langgraph_model(model)

    # Fail fast on missing provider credentials.
    if provider == "openai":
        _require_env("OPENAI_API_KEY")
    elif provider == "claude":
        _require_env("ANTHROPIC_API_KEY")
    elif provider == "langgraph":
        _require_langgraph_credentials(model)
    else:
        _require_env("GOOGLE_API_KEY")

    return RuntimeConfig(
        provider=provider,
        model=model,
        reasoning_effort=reasoning_effort,  # type: ignore[arg-type]
        verbosity=verbosity,  # type: ignore[arg-type]
        enable_web_search=enable_web_search,
        memory_dir=memory_dir,
        reports_dir=reports_dir,
        skills_project_dir=skills_project_dir,
        skills_user_dir=skills_user_dir,
        skills_builtin_dir=skills_builtin_dir,
    )
