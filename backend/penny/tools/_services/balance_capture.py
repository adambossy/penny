"""Daily account-balance capture: one appended sample per account per run.

The headless sibling of ``sync_service`` for balances: for every stored Plaid
item it pulls live balances (``PlaidClient.get_balances``), registers the
accounts, and appends one ``account_balances`` row per account. Registration
is self-healing — every account Plaid returns gets its name/type/subtype
refreshed, and accounts missing from ``plaid_accounts`` (an old backfill only
registered accounts seen in transactions) are inserted on the next run.

Failure semantics mirror sync: items are isolated, a broken connection is a
reported user-action line (``relink_required_items``) or a counted failure —
never an aborted run. Only an error outside the per-item loop (config, DB)
propagates to the caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from penny.adapters.clients.plaid import (
    BalanceAccount,
    PlaidClient,
    PlaidClientError,
    is_relink_error,
)
from penny.adapters.db.facade import DB


@dataclass
class BalanceCaptureSummary:
    """Aggregated results from capturing balances across all Plaid items."""

    accounts_captured: int = 0
    accounts_registered: int = 0  # newly inserted into plaid_accounts
    items_captured: int = 0
    items_failed: int = 0  # non-relink failures (transient/cipher/etc.)
    # Institutions whose Plaid item needs re-authentication — reported, not
    # raised, exactly as in SyncSummary.
    relink_required_items: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        result: dict[str, Any] = {
            "accounts_captured": self.accounts_captured,
            "accounts_registered": self.accounts_registered,
            "items_captured": self.items_captured,
            "items_failed": self.items_failed,
        }
        if self.relink_required_items:
            result["relink_required_items"] = self.relink_required_items
        return result


def _cents(value: float | None) -> int | None:
    """Plaid reports dollars as floats; the database speaks integer cents."""
    return None if value is None else round(value * 100)


def _balance_row(account: BalanceAccount, captured_at: datetime) -> dict[str, Any]:
    balances = account.balances
    return {
        "account_id": account.account_id,
        "captured_at": captured_at,
        "current_cents": _cents(balances.current),
        "available_cents": _cents(balances.available),
        "limit_cents": _cents(balances.limit),
        "currency": balances.iso_currency_code or balances.unofficial_currency_code,
    }


def capture_account_balances(
    db: DB,
    client: PlaidClient,
    *,
    captured_at: datetime | None = None,
) -> BalanceCaptureSummary:
    """Capture every linked account's current balances as append-only rows.

    One shared ``captured_at`` (UTC) stamps the whole run, so a day's samples
    line up when queried. Two per-item failures are routine and isolated: a
    Plaid re-auth error (reported in ``relink_required_items``) and a token
    the cipher can't decrypt in this environment (``ValueError``, counted in
    ``items_failed``).
    """
    captured_at = captured_at or datetime.now(UTC)
    summary = BalanceCaptureSummary()

    for item in db.list_plaid_items():
        label = item.institution_name or item.item_id
        try:
            accounts = client.get_balances(item.access_token)
        except (PlaidClientError, ValueError) as exc:
            logger.bind(item_id=item.item_id).warning(
                "Balance capture failed for item {} ({}): {}",
                item.item_id,
                label,
                exc,
            )
            if is_relink_error(exc):
                if label not in summary.relink_required_items:
                    summary.relink_required_items.append(label)
            else:
                summary.items_failed += 1
            continue

        summary.accounts_registered += db.register_plaid_accounts(
            item.item_id,
            [
                {
                    "account_id": account.account_id,
                    "name": account.name,
                    "type": account.type,
                    "subtype": account.subtype,
                }
                for account in accounts
            ],
        )
        summary.accounts_captured += db.add_account_balances(
            [_balance_row(account, captured_at) for account in accounts]
        )
        summary.items_captured += 1

    logger.info("Balance capture complete: {}", summary.to_dict())
    return summary
