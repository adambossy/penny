"""Regression tests for analytics tools exposed to the agent."""

from __future__ import annotations

from pathlib import Path

import pytest

from penny.tools.analytics import generate_chart


@pytest.mark.asyncio
async def test_generate_chart_accepts_the_public_label_to_number_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A flat category breakdown must not be mistaken for named series."""
    monkeypatch.chdir(tmp_path)

    result = await generate_chart.fn(
        chart_type="bar",
        title="Flagged spending",
        data={"Adult Education (flagged)": 42.50},
    )

    assert result["status"] == "success"
