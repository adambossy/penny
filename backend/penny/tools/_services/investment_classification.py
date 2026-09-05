"""Investment activity classification for reporting inclusion/exclusion.

Determines whether investment transactions should be included or excluded from
default spending analytics based on transaction type, subtype, and name.
"""

from __future__ import annotations

from typing import Literal

ReportingMode = Literal["DEFAULT_INCLUDE", "DEFAULT_EXCLUDE"]

# Keywords that indicate money movement (include in default analytics)
INCLUDE_KEYWORDS = [
    "zelle",
    "direct dep",
    "cash transfer",
    "payment",
    "ach",
    "wire",
    "check",
    "automated payment",
]

# Keywords that indicate investment income or trading activity (exclude from default)
EXCLUDE_KEYWORDS = [
    "dividend",
    "interest",
    "trade",
    "security",
    "margin",
    "distribution",
    "fx",
    "foreign exchange",
    "buy",
    "sell",
    # Corporate actions: the shares change, no money moves. They match none of
    # the trade words above ("Stock Split ... 2:1" is not a buy, a sell, or a
    # dividend), so without these they fell through to the INCLUDE default and
    # were put in front of the categorizer, which has nothing sensible to say
    # about them — it labeled a spin-off "investment_buy". Phrases, not the bare
    # word "split", so a payment that merely contains it is unaffected.
    "stock split",
    "reverse split",
    "split ratio",
    "spin-off",
    "spinoff",
    "spin off",
    "merger",
    "reorganization",
    "name change",
]


def investment_activity_reporting_mode(
    *,
    transaction_type: str | None,
    transaction_subtype: str | None,
    transaction_name: str,
) -> ReportingMode:
    """Classify investment activity for default reporting inclusion/exclusion.

    Args:
        transaction_type: Plaid investment transaction type field
        transaction_subtype: Plaid investment transaction subtype field
        transaction_name: Plaid investment transaction name field

    Returns:
        "DEFAULT_INCLUDE" for money movement (Zelle, ACH, direct deposit, etc.)
        "DEFAULT_EXCLUDE" for investment income/trade activity (dividends, trades, etc.)

    Classification logic:
    - Check transaction type/subtype for strong signals
    - Check name for keyword matches (case-insensitive)
    - Default to INCLUDE when no strong exclude signal exists (conservative approach)
    """
    # Normalize fields for comparison
    txn_type_lower = (transaction_type or "").lower()
    txn_subtype_lower = (transaction_subtype or "").lower()
    txn_name_lower = transaction_name.lower()

    # Combined text for keyword matching
    combined_text = f"{txn_type_lower} {txn_subtype_lower} {txn_name_lower}"

    # Check for exclude keywords (strong signal for investment activity)
    for keyword in EXCLUDE_KEYWORDS:
        if keyword in combined_text:
            return "DEFAULT_EXCLUDE"

    # Check for include keywords (money movement)
    for keyword in INCLUDE_KEYWORDS:
        if keyword in combined_text:
            return "DEFAULT_INCLUDE"

    # Default to INCLUDE for uncertain cases (conservative)
    # This ensures we don't accidentally hide real spending transactions
    return "DEFAULT_INCLUDE"
