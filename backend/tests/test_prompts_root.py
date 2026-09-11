"""The prompt store is pinned to ``backend/``, not to the process's CWD.

A front door can be launched from anywhere — the daemon's report job inherits
launchd's ``/``, which promptorium's CWD-walking discovery resolved to a
``/.prompts`` it then tried to create (read-only filesystem, job dead). Any
directory without repo markers reproduces that, so this loads a real prompt
from one.
"""

from __future__ import annotations

import os
from pathlib import Path

from penny.prompts import load_prompt, prompt_service


def test_prompts_resolve_from_a_cwd_with_no_repo_markers(tmp_path: Path) -> None:
    load_prompt.cache_clear()
    prompt_service.cache_clear()
    previous = Path.cwd()
    os.chdir(tmp_path)
    try:
        assert load_prompt("penny-system-prompt").strip()
        # Nothing was created beside the unmarked cwd.
        assert not (tmp_path / ".prompts").exists()
    finally:
        os.chdir(previous)
        load_prompt.cache_clear()
        prompt_service.cache_clear()
