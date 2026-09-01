"""Prompt loading is anchored to the package, not the process's CWD.

The regression these lock: promptorium's own loader finds `.prompts` by
walking up from the working directory *and creates it where it lands*, so a
CWD-relative load resolved differently per entrypoint — the launchd daemon
(CWD=`/`) died on `OSError: Read-only file system: '/.prompts'`, and a server
started from the repo root got a freshly-created EMPTY index and failed every
run with `PromptNotFound`. Penny has entrypoints whose CWD it cannot control
(`penny mcp` inherits the user's project directory), so the anchor is the only
fix that covers them (REQUIREMENTS T7).
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from penny.prompts import load_prompt, prompt_service

_KEY = "penny-system-prompt"


@pytest.fixture
def chdir_elsewhere(tmp_path: Path):
    """Run the test body from a directory that has no `.prompts` anywhere above."""
    original = Path.cwd()
    os.chdir(tmp_path)
    try:
        yield tmp_path
    finally:
        os.chdir(original)


def test_load_prompt_ignores_the_working_directory(chdir_elsewhere: Path):
    load_prompt.cache_clear()
    prompt_service.cache_clear()

    assert load_prompt(_KEY).strip()
    # The bug's signature was creation, not just failure: a miss silently
    # scattered an empty index into whatever directory the process sat in.
    assert not (chdir_elsewhere / ".prompts").exists()


def test_load_prompt_works_from_the_filesystem_root():
    """The daemon's actual CWD. `/` is read-only, so the old code raised OSError."""
    source = f"from penny.prompts import load_prompt;print(len(load_prompt({_KEY!r})))"
    result = subprocess.run(  # noqa: S603 - our own interpreter, literal source
        [sys.executable, "-c", source],
        cwd="/",
        capture_output=True,
        text=True,
        env={**os.environ, "PENNY_SENTRY_ENABLED": "false"},
    )

    assert result.returncode == 0, result.stderr
    assert int(result.stdout.strip()) > 0
