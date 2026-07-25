"""Report rendering: disagreement detection + self-contained HTML."""

from __future__ import annotations

from penny.eval.report import disagreements, render_eval_report

_ITEMS = [
    {
        "transaction_id": 1,
        "merchant_descriptor": "WHOLE FOODS",
        "amount": 50.0,
        "date": "2026-06-26",
        "legacy_key": "food.restaurants",
        "agent_key": "food.groceries",
        "agent_confidence": 0.9,
        "agent_reasoning": "grocery store",
        "method_at_eval_time": "agent",
        "tools_consulted": ["merchant_category_history", "submit_categorization"],
        "trace_link": "https://lf/trace/abc",
    },
    {
        "transaction_id": 2,
        "merchant_descriptor": "STARBUCKS",
        "amount": 6.0,
        "date": "2026-06-26",
        "legacy_key": "food.restaurants",
        "agent_key": "food.restaurants",
        "agent_confidence": 1.0,
        "agent_reasoning": "coffee",
        "method_at_eval_time": "fast_path",
        "tools_consulted": [],
        "trace_link": None,
    },
]


def test_disagreements() -> None:
    d = disagreements(_ITEMS)
    assert [it["transaction_id"] for it in d] == [1]


def test_render_is_self_contained_html() -> None:
    html_doc = render_eval_report(
        _ITEMS, run_at="2026-06-27T00:00:00", version={"model": "gemini-3.6-flash"}
    )
    assert html_doc.startswith("<!doctype html>")
    assert "Categorizer eval" in html_doc
    assert "1 disagreement(s)" in html_doc
    assert "model=gemini-3.6-flash" in html_doc
    # payload embedded for the pager
    assert "WHOLE FOODS" in html_doc
