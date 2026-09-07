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

# `backend/` — the root promptorium appends `.prompts` to. Resolved from THIS
# file rather than the process's working directory: promptorium's own
# `load_prompt` locates the directory with `find_repo_root()`, which walks up
# from the CWD *and creates what it doesn't find*. So the same call resolved
# differently per entrypoint — fine from `backend/`, but the launchd daemon
# (CWD=`/`) died on `OSError: Read-only file system: '/.prompts'`, and a server
# started from the repo root silently got a freshly-created EMPTY index and
# failed every run with PromptNotFound. Penny has entrypoints whose CWD it
# cannot control at all (`penny mcp` inherits the user's project directory),
# so anchoring is the only fix that covers them.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent


@cache
def prompt_service() -> PromptService:
    """The one promptorium service, rooted at ``backend/.prompts``.

    For callers needing more than the latest text, e.g. version lookup.
    ``@cache`` makes this a lazy singleton on purpose: ``PromptService``
    construction calls ``ensure_initialized()``, a filesystem side effect, so a
    module-level instance would run it at import of a module imported nearly
    everywhere — and per-caller instances re-run it, plus the ``_meta.json``
    read, on every call.
    """
    return PromptService(FileSystemPromptStorage(_BACKEND_ROOT))


@cache
def load_prompt(name: str) -> str:
    """Return the latest version of the named prompt.

    Backed by promptorium; raises ``promptorium.domain.PromptNotFound`` (or
    similar) if the key is missing. Cached per-process.
    """
    return prompt_service().load_prompt(name)
