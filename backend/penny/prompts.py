"""Single prompt loader for the whole backend.

Source of truth: ``backend/.prompts/<key>/<version>.md`` (promptorium's
managed-by-root layout). The index lives at ``.prompts/_meta.json``.

Every consumer — ``agent_factory`` (system prompt), the categorizer
(categorize-transactions, taxonomy-rules), reports, etc. — reads through
this single function so there's exactly one prompt directory and one
loader semantics.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

from promptorium import FileSystemPromptStorage, PromptService

# The prompt root, resolved from THIS file rather than the process's working
# directory. promptorium's module-level `load_prompt` locates `.prompts` via
# `find_repo_root()`, which walks up from the CWD — so the same call resolved
# differently per entrypoint: fine from `backend/`, but the launchd daemon runs
# with CWD=`/` and got `OSError: Read-only file system: '/.prompts'`, and a
# server started from the repo root silently got an EMPTY index (promptorium
# creates `.prompts/` where it looks), failing every run with PromptNotFound.
# Anchoring here makes the loader CWD-independent: `backend/.prompts` is the
# single source of truth this module's docstring already claims it is.
_PROMPT_ROOT = Path(__file__).resolve().parent.parent


@cache
def prompt_service() -> PromptService:
    """The one promptorium service, rooted at ``backend/.prompts``.

    Callers needing more than the latest text — version lookup, for instance —
    take the service from here rather than building their own from
    ``find_repo_root()``, which would reintroduce the CWD dependence this
    module exists to remove.
    """
    return PromptService(FileSystemPromptStorage(_PROMPT_ROOT))


@cache
def load_prompt(name: str) -> str:
    """Return the latest version of the named prompt.

    Backed by promptorium; raises ``promptorium.domain.PromptNotFound`` (or
    similar) if the key is missing. Cached per-process.
    """
    return prompt_service().load_prompt(name)
