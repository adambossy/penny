"""Which investment activity counts as spending, and which is just the market.

``DEFAULT_INCLUDE`` means "real money moved, categorize it and count it in
spending"; ``DEFAULT_EXCLUDE`` means "this is the portfolio doing portfolio
things". The classifier is keyword-based over Plaid's type/subtype/name, and
falls through to INCLUDE when nothing matches — deliberately, so a real
payment is never silently hidden.

The money-movement cases below are the ones that must never regress: a
mortgage payment or a five-figure transfer wrongly excluded would vanish from
spending analytics and from review, which is far worse than a stray corporate
action showing up.
"""

from __future__ import annotations

import pytest

from penny.tools._services.investment_classification import (
    investment_activity_reporting_mode as classify,
)

# Real descriptors from the Bossy household's brokerage accounts.
MONEY_MOVEMENT = [
    "Automated Payment MORGAN STANLEY CHK ACCT ENDING IN 1729",
    "Automated Payment ALLIANT CU       XFER CHK ACCT ENDING IN 1729",
    "CASH TRANSFER FUNDS TRANSFERRED Confirmation - #DDVCXJR46 To XXX-XX9320",
    "Zelle Payment TO TANIA",
    "Direct Deposit DIRECT DEP FUNDS RECVD",
    "Wire Transfer Incoming",
    "Check Deposit",
]

CORPORATE_ACTIONS = [
    "Stock Split AMPHENOL CORP NEW CL A SPLIT RATIO  2:1",
    "Pending Stock Spin-Off MOBILITY GLOBAL INC",
    "Reverse Split ACME CORP",
    "Merger XYZ INTO ABC",
    "Name Change OLD CO TO NEW CO",
]

# Income matches on the name; trades do NOT — "Bought"/"Sold" contain neither
# "buy" nor "sell" as substrings, so their exclusion rides entirely on Plaid's
# subtype, which is how sync supplies it (verified: all 1,481 Bought/Sold rows
# in the real data are excluded). Pinned here so the dependency is visible.
TRADING_AND_INCOME = [
    ("Bought APPLE INC", "buy"),
    ("Sold WALT DISNEY CO HLDG CO", "sell"),
    ("Qualified Dividend BROADCOM INC", None),
    ("Interest Income MORGAN STANLEY PRIVATE BANK NA", None),
]


@pytest.mark.parametrize("name", MONEY_MOVEMENT)
def test_money_movement_stays_included(name: str) -> None:
    """The high-value rows: a mortgage payment or transfer must never be hidden."""
    assert (
        classify(transaction_type=None, transaction_subtype=None, transaction_name=name)
        == "DEFAULT_INCLUDE"
    )


@pytest.mark.parametrize("name", CORPORATE_ACTIONS)
def test_corporate_actions_are_excluded(name: str) -> None:
    """Shares change, no money moves — nothing for the categorizer to decide."""
    assert (
        classify(transaction_type=None, transaction_subtype=None, transaction_name=name)
        == "DEFAULT_EXCLUDE"
    )


@pytest.mark.parametrize(("name", "subtype"), TRADING_AND_INCOME)
def test_trades_and_income_are_excluded(name: str, subtype: str | None) -> None:
    assert (
        classify(
            transaction_type=None,
            transaction_subtype=subtype,
            transaction_name=name,
        )
        == "DEFAULT_EXCLUDE"
    )


def test_a_trade_without_plaid_subtype_is_not_caught_by_name() -> None:
    """Documents a real gap rather than asserting it is fine.

    "Bought"/"Sold" are not matched by the "buy"/"sell" keywords, so a trade
    arriving without Plaid's subtype would be treated as money movement and
    land in the review queue. It has not happened — every Bought/Sold row in
    the data carries the subtype — but the exclusion rests on that field
    alone, and this test is where that shows up if it ever changes.
    """
    assert (
        classify(
            transaction_type=None,
            transaction_subtype=None,
            transaction_name="Bought APPLE INC",
        )
        == "DEFAULT_INCLUDE"
    )


def test_a_payment_containing_split_is_still_money_movement() -> None:
    """Corporate actions match on phrases, so a "split" payment is unaffected.

    A bare "split" keyword would have caught this one, which is exactly the
    kind of real money the INCLUDE default exists to protect.
    """
    assert (
        classify(
            transaction_type=None,
            transaction_subtype=None,
            transaction_name="Automated Payment SPLIT RENT TO ROOMMATE",
        )
        == "DEFAULT_INCLUDE"
    )


def test_unknown_activity_defaults_to_included() -> None:
    """Unrecognized activity is kept: hiding real spending is the worse error."""
    assert (
        classify(
            transaction_type=None,
            transaction_subtype=None,
            transaction_name="Some Unrecognized Brokerage Line",
        )
        == "DEFAULT_INCLUDE"
    )
