#!/usr/bin/env python3
"""Report the current balance of every linked Plaid account.

Read-only reconnaissance ahead of the daily balance-snapshot job. For each
stored Plaid item it calls ``/accounts/balance/get`` and prints one row per
account, so we can see what Plaid *actually* returns for this user's real
institutions — which fields are populated, which banks omit ``available`` or
``limit``, whether ``last_updated_datetime`` is ever set — before committing to
a snapshot schema.

Nothing is written: no Plaid mutation, no database write. It reads
``plaid_items`` and calls one read endpoint per item.

Usage (from backend/):
  uv run python scripts/account_balances.py           # table (default)
  uv run python scripts/account_balances.py --json    # raw rows, for piping

``DATABASE_URL`` and the ``PLAID_*`` credentials come from ``backend/.env``,
i.e. the same connection the local server uses; the banner names the host and
Plaid environment so there is never any doubt which data you are looking at.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import sys

from dotenv import load_dotenv

# Importable as a plain script from backend/ without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

load_dotenv(override=False)

from penny.adapters.clients.plaid import (  # noqa: E402
    PlaidClient,
    PlaidClientError,
)
from penny.adapters.db.facade import DB  # noqa: E402


@dataclass
class BalanceRow:
    """One account's balances, flattened for display."""

    institution: str
    account: str
    mask: str
    kind: str
    currency: str
    current: float | None
    available: float | None
    limit: float | None
    as_of: str
    error: str | None = None


def _collect(db: DB, client: PlaidClient) -> tuple[list[BalanceRow], list[str]]:
    """Fetch balances for every stored item.

    A broken item is reported as its own row rather than aborting the run — a
    single bad connection should not hide the balances of every healthy one.
    Two failures are routine enough to expect: ``ITEM_LOGIN_REQUIRED`` from
    Plaid, and a ``ValueError`` from the token cipher when this environment has
    no ``PENNY_PLAID_TOKEN_KEY`` for the version the stored token was written
    under.
    """
    rows: list[BalanceRow] = []
    notes: list[str] = []

    for item in db.list_plaid_items():
        institution = item.institution_name or item.item_id[:12]
        try:
            accounts = client.get_balances(item.access_token)
        except (PlaidClientError, ValueError) as exc:
            notes.append(f"{institution}: {exc}")
            rows.append(
                BalanceRow(
                    institution=institution,
                    account="—",
                    mask="",
                    kind="",
                    currency="",
                    current=None,
                    available=None,
                    limit=None,
                    as_of="",
                    error=str(exc)[:120],
                )
            )
            continue

        for account in accounts:
            balances = account.balances
            kind = "/".join(p for p in (account.type, account.subtype) if p)
            rows.append(
                BalanceRow(
                    institution=institution,
                    account=account.name,
                    mask=account.mask or "",
                    kind=kind,
                    currency=(
                        balances.iso_currency_code
                        or balances.unofficial_currency_code
                        or ""
                    ),
                    current=balances.current,
                    available=balances.available,
                    limit=balances.limit,
                    as_of=balances.last_updated_datetime or "",
                )
            )

    rows.sort(key=lambda r: (r.institution.lower(), r.account.lower()))
    return rows, notes


def _money(value: float | None) -> str:
    return "" if value is None else f"{value:,.2f}"


def _render_table(rows: list[BalanceRow]) -> str:
    headers = [
        "INSTITUTION",
        "ACCOUNT",
        "MASK",
        "TYPE",
        "CUR",
        "CURRENT",
        "AVAILABLE",
        "LIMIT",
        "AS OF",
    ]
    numeric = {5, 6, 7}

    body = [
        [
            row.institution,
            row.account if row.error is None else f"{row.account} [{row.error}]",
            row.mask,
            row.kind,
            row.currency,
            _money(row.current),
            _money(row.available),
            _money(row.limit),
            row.as_of,
        ]
        for row in rows
    ]

    widths = [
        max(len(headers[i]), *(len(r[i]) for r in body)) if body else len(headers[i])
        for i in range(len(headers))
    ]

    def line(cells: list[str]) -> str:
        return "  ".join(
            cell.rjust(widths[i]) if i in numeric else cell.ljust(widths[i])
            for i, cell in enumerate(cells)
        ).rstrip()

    out = [line(headers), "  ".join("-" * w for w in widths)]
    out.extend(line(cells) for cells in body)
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json", action="store_true", help="emit rows as JSON instead of a table"
    )
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL is not set (backend/.env).", file=sys.stderr)
        return 2

    host = database_url.split("@")[-1].split("/")[0]
    print(
        f">> db {host} | plaid {os.environ.get('PLAID_ENV', 'sandbox')} | read-only",
        file=sys.stderr,
    )

    db = DB(database_url)
    client = PlaidClient.from_env()

    rows, notes = _collect(db, client)
    if not rows:
        print("No Plaid items found — nothing to report.", file=sys.stderr)
        return 0

    if args.json:
        print(json.dumps([asdict(row) for row in rows], indent=2))
        return 0

    print(_render_table(rows))

    healthy = [r for r in rows if r.error is None]
    print(
        f"\n{len(healthy)} account(s) across "
        f"{len({r.institution for r in rows})} institution(s).",
        file=sys.stderr,
    )
    print(
        "Credit/loan balances are positive amounts OWED; blank cells are "
        "fields the institution did not report.",
        file=sys.stderr,
    )
    for note in notes:
        print(f"  ! {note}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
