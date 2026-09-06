"""The accuracy-by-day chart on the scoreboard."""

from __future__ import annotations

from datetime import datetime, timedelta

from penny.api.review_page import _accuracy_chart


def _item(day: str, correct: bool) -> dict[str, str]:
    return {
        "synced_on": day,
        "agent_key": "food.groceries",
        "human_key": "food.groceries" if correct else "food.restaurants",
    }


def _day(offset: int) -> str:
    return (datetime.now().date() - timedelta(days=offset)).isoformat()


def test_bars_report_each_days_accuracy() -> None:
    svg = _accuracy_chart([_item(_day(1), True)] * 3 + [_item(_day(1), False)])

    assert "<svg" in svg
    assert f"{_day(1)}: 3/4 correct (75%)" in svg


def test_days_are_separate_bars() -> None:
    svg = _accuracy_chart([_item(_day(1), True), _item(_day(2), False)])

    assert svg.count("<rect") == 2
    assert "1/1 correct (100%)" in svg
    assert "0/1 correct (0%)" in svg


def test_days_outside_the_window_are_dropped() -> None:
    """Otherwise one ancient labelled day squashes the recent ones."""
    svg = _accuracy_chart([_item(_day(90), True), _item(_day(2), True)], days=60)

    assert svg.count("<rect") == 1
    assert _day(90) not in svg


def test_an_all_wrong_day_is_still_drawn() -> None:
    """0% must not render as nothing — that reads as "no data", not "all wrong"."""
    svg = _accuracy_chart([_item(_day(1), False)])

    assert "0/1 correct (0%)" in svg
    height = float(svg.split('height="')[2].split('"')[0])
    assert height > 0


def test_no_labelled_days_says_so_instead_of_drawing_an_empty_axis() -> None:
    assert "<svg" not in _accuracy_chart([])
    assert "No labelled transactions" in _accuracy_chart([])
