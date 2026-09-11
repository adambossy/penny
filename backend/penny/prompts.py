"""Single prompt loader for the whole backend.

Source of truth: ``backend/.prompts/<key>/<version>.md`` (promptorium's
managed-by-root layout). The index lives at ``.prompts/_meta.json``.

Every consumer — ``agent_factory`` (system prompt), the categorizer
(categorize-transactions, taxonomy-rules), reports, etc. — reads through
this single function so there's exactly one prompt directory and one
loader semantics.

The root is pinned to ``backend/`` from this file's own location.
promptorium's convenience API instead discovers a root by walking up from
the *current working directory*, which makes the prompt store depend on
where the process was launched: the daemon's report job inherits launchd's
``/`` and resolved ``/.prompts`` (read-only filesystem, job dead), while a
run from the repo root silently created a second, empty ``.prompts/`` there.
A front door may be started from anywhere; the prompts never move.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

from promptorium import PromptService
from promptorium.storage import FileSystemPromptStorage

# backend/penny/prompts.py -> backend/
_BACKEND_ROOT = Path(__file__).resolve().parent.parent


@cache
def prompt_service() -> PromptService:
    """The backend's promptorium service, rooted at ``backend/.prompts``."""
    return PromptService(FileSystemPromptStorage(_BACKEND_ROOT))


@cache
def load_prompt(name: str) -> str:
    """Return the latest version of the named prompt.

    Backed by promptorium; raises ``promptorium.domain.PromptNotFound`` (or
    similar) if the key is missing. Cached per-process.
    """
    return prompt_service().load_prompt(name)
