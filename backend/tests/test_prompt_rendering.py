"""The rendered system prompt fills every placeholder."""

from __future__ import annotations

from datetime import date
import re

from penny.agent_factory import _render_system_prompt
from penny.bootstrap import bootstrap


def test_render_system_prompt_fills_all_placeholders(isolated_db, isolated_workspace):
    bootstrap()

    output = _render_system_prompt()

    assert re.findall(r"\{\{[A-Z_]+\}\}", output) == []
    assert date.today().isoformat() in output
    assert "Penny" in output


def test_render_system_prompt_inlines_memory_files(isolated_db, isolated_workspace):
    memory_dir = isolated_workspace / "memory"
    memory_dir.mkdir()
    (memory_dir / "merchant-rules.md").write_text("## Rule: Test Sentinel Rule\n")
    bootstrap()

    output = _render_system_prompt()

    assert "Test Sentinel Rule" in output
