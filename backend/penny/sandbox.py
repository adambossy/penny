"""Per-process workspace sandbox.

One ``InProcessSandbox`` rooted at the user's workspace directory
(``~/.penny`` by default). Tools that need filesystem or shell access
read this singleton.
"""

from __future__ import annotations

from agent_harness.sandboxes.inprocess import InProcessSandbox

from .workspace import resolve_workspace_dir

_sandbox: InProcessSandbox | None = None


def get_sandbox() -> InProcessSandbox:
    global _sandbox
    if _sandbox is None:
        root = resolve_workspace_dir()
        root.mkdir(parents=True, exist_ok=True)
        _sandbox = InProcessSandbox(root=str(root))
    return _sandbox
