from __future__ import annotations

from collections.abc import Callable
from datetime import date
import json
import os
from typing import Any, Literal, Self, TypedDict, cast
import urllib.error
import urllib.parse
import urllib.request

from pydantic import BaseModel, Field

from penny.adapters.clients.plaid_link import (
    PublicTokenTimeoutError,
    build_success_message,
    clear_link_token_file,
    clear_token_file,
    create_link_token_and_url,
    exchange_token_and_get_item_info,
    is_port_in_use,
    open_link_in_browser,
    save_item_to_database,
    wait_for_token_from_file,
)
from penny.adapters.clients.plaid_models import PersonalFinanceCategory, Transaction
from penny.adapters.db.models import PlaidItem

PlaidEnv = Literal["sandbox", "development", "production"]


class PlaidClientError(Exception):
    """Base error for Plaid client failures."""


# Plaid item error_codes that mean "the user must re-authenticate (relink) this
# bank connection" — as opposed to a transient/network blip. Owned here so every
# consumer (sync, connection-status tool, balance capture) classifies the wire
# format the same way.
RELINK_ERROR_CODES = frozenset(
    {
        "ITEM_LOGIN_REQUIRED",
        "PENDING_EXPIRATION",
        "INVALID_CREDENTIALS",
        "INVALID_MFA",
        "ITEM_LOCKED",
        "USER_PERMISSION_REVOKED",
    }
)


def is_relink_error(exc: BaseException) -> bool:
    """True when ``exc`` carries a Plaid re-auth error code (relink needed)."""
    return any(code in str(exc) for code in RELINK_ERROR_CODES)


PLAID_ENV_MAP: dict[PlaidEnv, str] = {
    "sandbox": "https://sandbox.plaid.com",
    "development": "https://development.plaid.com",
    "production": "https://production.plaid.com",
}


class PlaidAccount(TypedDict):
    account_id: str
    name: str
    official_name: str | None
    mask: str | None
    subtype: str | None
    type: str | None
    institution: str | None


class PlaidItemInfo(TypedDict):
    item_id: str
    institution_id: str | None
    institution_name: str | None


class PlaidBaseModel(BaseModel):
    """Shared base for Plaid response models with a short parse alias."""

    @classmethod
    def parse(cls, data: Any) -> Self:
        return cls.model_validate(data)


class LinkTokenCreateResponse(PlaidBaseModel):
    link_token: str


class AccountsGetAccount(PlaidBaseModel):
    account_id: str
    name: str
    official_name: str | None = None
    mask: str | None = None
    subtype: str | None = None
    type: str | None = None

    def to_typed(self) -> PlaidAccount:
        return {
            "account_id": self.account_id,
            "name": self.name,
            "official_name": self.official_name,
            "mask": self.mask,
            "subtype": self.subtype,
            "type": self.type,
            # Institution name can be resolved separately via get_item_info().
            "institution": None,
        }


class AccountsGetResponse(PlaidBaseModel):
    accounts: list[AccountsGetAccount]


class AccountBalances(PlaidBaseModel):
    """An account's point-in-time balances, as Plaid reports them.

    Sign follows the account type: depository/investment balances are positive
    holdings, while credit and loan balances are positive amounts *owed*.
    ``available`` and ``limit`` are frequently null — plenty of institutions
    report neither.
    """

    available: float | None = None
    current: float | None = None
    limit: float | None = None
    iso_currency_code: str | None = None
    unofficial_currency_code: str | None = None
    # When the institution last refreshed these figures. Plaid only populates
    # it for investment accounts and a subset of institutions.
    last_updated_datetime: str | None = None


class BalanceAccount(PlaidBaseModel):
    account_id: str
    name: str
    official_name: str | None = None
    mask: str | None = None
    subtype: str | None = None
    type: str | None = None
    balances: AccountBalances = Field(default_factory=AccountBalances)


class AccountsBalanceGetResponse(PlaidBaseModel):
    accounts: list[BalanceAccount] = Field(default_factory=list)


class ItemModel(PlaidBaseModel):
    item_id: str
    institution_id: str | None = None
    # Plaid sets `error` to a non-null error object (with `error_code`) when the
    # item is unhealthy — most importantly ITEM_LOGIN_REQUIRED, meaning the user
    # must re-authenticate (relink) before syncs resume. Null when healthy.
    error: dict[str, Any] | None = None


class ItemGetResponse(PlaidBaseModel):
    item: ItemModel


class InstitutionModel(PlaidBaseModel):
    name: str | None = None


class InstitutionGetByIdResponse(PlaidBaseModel):
    institution: InstitutionModel | None = None


class AccountWithInstitution(PlaidBaseModel):
    """Account information combined with institution and item details."""

    account_id: str
    name: str
    official_name: str | None = None
    mask: str | None = None
    subtype: str | None = None
    type: str | None = None
    institution_name: str | None = None
    institution_id: str | None = None
    item_id: str


class PlaidTransactionModel(PlaidBaseModel):
    transaction_id: str | None = None
    account_id: str
    amount: float
    iso_currency_code: str | None = None
    date: str
    name: str
    merchant_name: str | None = None
    original_description: str | None = None
    pending: bool = False
    payment_channel: str | None = None
    unofficial_currency_code: str | None = None
    category: list[str] | None = None
    category_id: str | None = None
    personal_finance_category: dict[str, Any] | None = None
    counterparties: list[dict[str, Any]] | None = None

    def to_typed(self) -> Transaction:
        txn: Transaction = {
            "transaction_id": self.transaction_id,
            "account_id": self.account_id,
            "amount": self.amount,
            "iso_currency_code": self.iso_currency_code,
            "date": self.date,
            "name": self.name,
            "merchant_name": self.merchant_name,
            # Plaid's field is `original_description`; internally we use the
            # `_descriptor` suffix to match `merchant_descriptor`.
            "original_descriptor": self.original_description,
            "pending": self.pending,
            "payment_channel": self.payment_channel,
            "unofficial_currency_code": self.unofficial_currency_code,
            "category": self.category,
            "category_id": self.category_id,
            "personal_finance_category": cast(
                PersonalFinanceCategory | None, self.personal_finance_category
            ),
            "counterparties": self.counterparties,
        }
        return txn


class TransactionsGetResponse(PlaidBaseModel):
    transactions: list[PlaidTransactionModel] = Field(default_factory=list)


class TransactionsSyncResponse(PlaidBaseModel):
    added: list[PlaidTransactionModel] = Field(default_factory=list)
    modified: list[PlaidTransactionModel] = Field(default_factory=list)
    removed: list[dict[str, Any]] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False

    def to_sync_result(self, *, fallback_cursor: str | None) -> dict[str, Any]:
        return {
            "added": [txn.to_typed() for txn in self.added],
            "modified": [txn.to_typed() for txn in self.modified],
            "removed": self.removed,
            "next_cursor": self.next_cursor or (fallback_cursor or ""),
            "has_more": self.has_more,
        }


class PlaidInvestmentSecurity(PlaidBaseModel):
    """Investment security model for Plaid."""

    security_id: str
    name: str | None = None
    ticker_symbol: str | None = None
    type: str | None = None


class PlaidInvestmentTransaction(PlaidBaseModel):
    """Investment transaction model from Plaid."""

    investment_transaction_id: str
    account_id: str
    amount: float
    date: str
    name: str
    quantity: float | None = None
    price: float | None = None
    fees: float | None = None
    type: str | None = None
    subtype: str | None = None
    security_id: str | None = None
    iso_currency_code: str | None = None
    unofficial_currency_code: str | None = None


class InvestmentTransactionsGetResponse(PlaidBaseModel):
    """Response from /investments/transactions/get endpoint."""

    investment_transactions: list[PlaidInvestmentTransaction] = Field(
        default_factory=list
    )
    securities: list[PlaidInvestmentSecurity] = Field(default_factory=list)
    total_investment_transactions: int = 0


class PlaidInvestmentHolding(PlaidBaseModel):
    """Investment holding model from Plaid."""

    account_id: str
    security_id: str
    institution_price: float | None = None
    institution_value: float | None = None
    quantity: float
    cost_basis: float | None = None
    iso_currency_code: str | None = None


class InvestmentHoldingsGetResponse(PlaidBaseModel):
    """Response from /investments/holdings/get endpoint."""

    holdings: list[PlaidInvestmentHolding] = Field(default_factory=list)
    securities: list[PlaidInvestmentSecurity] = Field(default_factory=list)


class PlaidClient:
    def __init__(
        self,
        *,
        client_id: str,
        secret: str,
        env: PlaidEnv = "sandbox",
        client_name: str = "penny",
        products: list[str] | None = None,
    ) -> None:
        self._client_id = client_id
        self._secret = secret
        self._env = env
        self._client_name = client_name
        self._products = products or []

    @property
    def env(self) -> PlaidEnv:
        return self._env

    @classmethod
    def from_env(cls) -> PlaidClient:
        """Construct a PlaidClient from environment variables.

        Required:
        - PLAID_CLIENT_ID
        - PLAID_ENV (defaults to sandbox)
        - PLAID_<ENV>_SECRET (e.g. PLAID_SANDBOX_SECRET)
        """
        env_str = os.getenv("PLAID_ENV", "sandbox").lower()
        if env_str not in PLAID_ENV_MAP:
            raise PlaidClientError(
                f"Invalid PLAID_ENV={env_str!r}. "
                "Expected one of: sandbox, development, production."
            )
        env: PlaidEnv = env_str  # type: ignore[assignment]

        client_id = cls._getenv_or_die("PLAID_CLIENT_ID")
        secret = cls._secret_from_env(env)
        client_name = os.getenv("PLAID_CLIENT_NAME", "penny")
        return cls(client_id=client_id, secret=secret, env=env, client_name=client_name)

    @staticmethod
    def _getenv_or_die(name: str) -> str:
        value = os.getenv(name)
        if not value:
            raise PlaidClientError(f"Missing required environment variable: {name}")
        return value

    @classmethod
    def _secret_from_env(cls, env: PlaidEnv) -> str:
        if env == "production":
            return cls._getenv_or_die("PLAID_PRODUCTION_SECRET")
        if env == "development":
            return cls._getenv_or_die("PLAID_DEVELOPMENT_SECRET")
        if env == "sandbox":
            return cls._getenv_or_die("PLAID_SANDBOX_SECRET")
        raise PlaidClientError(f"Invalid PLAID_ENV={env!r}")

    def _base_url(self) -> str:
        try:
            return PLAID_ENV_MAP[self._env]
        except KeyError as e:
            raise PlaidClientError(
                f"Unsupported Plaid environment: {self._env!r}"
            ) from e

    def _parse_json_response(self, body: str) -> dict[str, Any]:
        """Parse JSON response from Plaid API.

        Args:
            body: JSON response body as string

        Returns:
            Parsed JSON as dictionary

        Raises:
            PlaidClientError: If JSON parsing fails
        """
        try:
            return cast(dict[str, Any], json.loads(body))
        except json.JSONDecodeError as e:
            raise PlaidClientError(
                f"Failed to parse Plaid response as JSON: {e}: {body}"
            ) from e

    @staticmethod
    def _with_decrypted_token(payload: dict[str, Any]) -> dict[str, Any]:
        """Decrypt an at-rest-encrypted access_token right before the wire.

        The single decryption point: tokens stay encrypted in the DB and in
        memory (see the facade's ``_stored_token``); only the outgoing Plaid
        request carries plaintext. Plaintext tokens (dev without the key)
        pass through.
        """
        token = payload.get("access_token")
        if not isinstance(token, str):
            return payload
        from penny.security.token_cipher import decrypt_token, is_encrypted

        if not is_encrypted(token):
            return payload
        return {**payload, "access_token": decrypt_token(token)}

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = self._base_url().rstrip("/") + path
        data = json.dumps(self._with_decrypted_token(payload)).encode("utf-8")
        req = urllib.request.Request(  # noqa: S310
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req) as resp:  # noqa: S310 - external HTTPS
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:  # pragma: no cover - network-dependent
            err_body = e.read().decode("utf-8", "ignore")
            raise PlaidClientError(f"Plaid API error ({e.code}): {err_body}") from e
        except urllib.error.URLError as e:  # pragma: no cover - network-dependent
            raise PlaidClientError(f"Network error calling Plaid API: {e}") from e

        return self._parse_json_response(body)

    # High-level APIs -----------------------------------------------------

    def create_link_token(
        self,
        *,
        user_id: str,
        redirect_uri: str | None = None,
        products: list[str] | None = None,
        required_if_supported_products: list[str] | None = None,
        additional_consented_products: list[str] | None = None,
        access_token: str | None = None,
        country_codes: list[str] | None = None,
        language: str = "en",
        client_name: str | None = None,
        days_requested: int = 730,
    ) -> str:
        """Create a Plaid Link token and return it.

        Args:
            user_id: User identifier for Plaid Link
            redirect_uri: OAuth redirect URI (if using redirect mode)
            products: Required products list (e.g. ["transactions"])
            required_if_supported_products: Products to request if institution supports
                them (e.g. ["investments"]) - for initial Link sessions
            additional_consented_products: Products to add consent for in update mode
                (e.g. ["investments"]) - use instead of required_if_supported_products
                when access_token is provided
            access_token: Existing access token for update mode (consent addition)
            country_codes: Country codes for Link (default: ["US"])
            language: Language for Link UI (default: "en")
            client_name: Display name for Link
            days_requested: Number of days of transaction history to request.
                Defaults to 730 (maximum ~2 years). Only applies on initial
                Item setup; cannot be changed after transactions are initialized.
        """
        # Update mode (re-auth / consent addition) is signalled by an existing
        # access_token; Plaid rejects a non-empty `products` list in that mode, so
        # force it empty and let `additional_consented_products` carry any new
        # consent. New-link sessions send the requested products as usual.
        update_mode = access_token is not None
        payload: dict[str, Any] = {
            "client_id": self._client_id,
            "secret": self._secret,
            "client_name": client_name or self._client_name,
            "language": language,
            "country_codes": country_codes or ["US"],
            "user": {"client_user_id": user_id},
            "products": [] if update_mode else (products or self._products or []),
            "transactions": {"days_requested": days_requested},
        }
        if redirect_uri is not None:
            payload["redirect_uri"] = redirect_uri
        if required_if_supported_products is not None:
            payload["required_if_supported_products"] = required_if_supported_products
        if additional_consented_products is not None:
            payload["additional_consented_products"] = additional_consented_products
        if access_token is not None:
            payload["access_token"] = access_token

        resp = LinkTokenCreateResponse.parse(self._post("/link/token/create", payload))
        return resp.link_token

    def exchange_public_token(self, public_token: str) -> dict[str, Any]:
        """Exchange a Link public_token for an access_token."""
        payload = {
            "client_id": self._client_id,
            "secret": self._secret,
            "public_token": public_token,
        }
        return self._post("/item/public_token/exchange", payload)

    def get_transactions_raw(
        self,
        access_token: str,
        *,
        start_date: date,
        end_date: date,
        count: int = 100,
    ) -> dict[str, Any]:
        """Call /transactions/get and return the raw Plaid response."""
        payload: dict[str, Any] = {
            "client_id": self._client_id,
            "secret": self._secret,
            "access_token": access_token,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "options": {
                "count": count,
                "offset": 0,
            },
        }
        return self._post("/transactions/get", payload)

    def get_accounts(self, access_token: str) -> list[PlaidAccount]:
        """Return accounts for an item using Plaid's /accounts/get endpoint."""
        payload: dict[str, Any] = {
            "client_id": self._client_id,
            "secret": self._secret,
            "access_token": access_token,
        }
        resp = AccountsGetResponse.parse(self._post("/accounts/get", payload))
        return [account.to_typed() for account in resp.accounts]

    def get_balances(self, access_token: str) -> list[BalanceAccount]:
        """Return every account on an item together with its current balances.

        Uses /accounts/balance/get rather than /accounts/get: the former forces
        a live balance pull from the institution, while the latter may serve
        Plaid's cached copy from the last transactions update. That freshness is
        the whole point for balance capture, and it costs an extra round trip to
        the bank — expect this call to be slow and to fail for items needing
        re-authentication.
        """
        payload: dict[str, Any] = {
            "client_id": self._client_id,
            "secret": self._secret,
            "access_token": access_token,
        }
        resp = AccountsBalanceGetResponse.parse(
            self._post("/accounts/balance/get", payload)
        )
        return resp.accounts

    def get_item_info(self, access_token: str) -> PlaidItemInfo:
        """Return item and institution information for an access token."""
        payload: dict[str, Any] = {
            "client_id": self._client_id,
            "secret": self._secret,
            "access_token": access_token,
        }
        item_resp = ItemGetResponse.parse(self._post("/item/get", payload))

        item_id = item_resp.item.item_id
        institution_id = item_resp.item.institution_id
        institution_name: str | None = None

        if institution_id:
            inst_payload: dict[str, Any] = {
                "client_id": self._client_id,
                "secret": self._secret,
                "institution_id": institution_id,
                "country_codes": ["US"],
            }
            inst_resp = InstitutionGetByIdResponse.parse(
                self._post("/institutions/get_by_id", inst_payload)
            )
            if inst_resp.institution and inst_resp.institution.name:
                institution_name = inst_resp.institution.name

        info: PlaidItemInfo = {
            "item_id": item_id,
            "institution_id": institution_id,
            "institution_name": institution_name,
        }
        return info

    def get_item_status(self, access_token: str) -> str | None:
        """Return the item's current Plaid error_code, or ``None`` when healthy.

        A single /item/get call — no institution lookup (callers already hold the
        institution name from the stored ``PlaidItem``). A non-null result (e.g.
        ``"ITEM_LOGIN_REQUIRED"``) means the connection needs re-authentication.
        """
        payload: dict[str, Any] = {
            "client_id": self._client_id,
            "secret": self._secret,
            "access_token": access_token,
        }
        item_resp = ItemGetResponse.parse(self._post("/item/get", payload))
        error = item_resp.item.error
        return error.get("error_code") if error else None

    def sync_transactions(
        self,
        access_token: str,
        *,
        cursor: str | None = None,
        count: int = 500,
    ) -> dict[str, Any]:
        """Thin wrapper around Plaid's /transactions/sync endpoint."""
        payload: dict[str, Any] = {
            "client_id": self._client_id,
            "secret": self._secret,
            "access_token": access_token,
            "count": count,
        }
        if cursor is not None:
            payload["cursor"] = cursor

        resp = TransactionsSyncResponse.parse(self._post("/transactions/sync", payload))
        return resp.to_sync_result(fallback_cursor=cursor)

    def connect_new_account(
        self,
        *,
        db: Any,  # DB type from services.db, avoiding circular import
        timeout_seconds: int = 300,
    ) -> dict[str, Any]:
        """Connect a new bank account via Plaid Link.

        Opens a browser window for the user to link their bank account via Plaid Link.
        The function handles the full OAuth flow, exchanges the public token for an
        access token, and stores the connection in the database.

        IMPORTANT: Requires an external redirect server to be running on port 8443.
        Start the server with: `penny plaid-serve`

        Args:
            db: Database instance with save_plaid_item method
            timeout_seconds: Timeout for waiting for Plaid Link completion
                (default: 300)

        Returns:
            Dictionary with connection status including:
            - status: "success" or "error"
            - item_id: Plaid item ID if successful
            - institution_name: Institution name if available
            - message: Human-readable status message
        """
        # Require external redirect server to be running
        if not is_port_in_use("localhost", 8443):
            return {
                "status": "error",
                "message": (
                    "Plaid redirect server not running. "
                    "Please start it with: penny plaid-serve"
                ),
            }

        redirect_uri = "https://localhost:8443/plaid-link-complete"
        clear_token_file()  # Clear any stale token
        state: dict[str, Any] = {}

        try:
            # Create Link token and URL with investments consent
            link_url = create_link_token_and_url(
                redirect_uri=redirect_uri,
                state=state,
                create_link_token_fn=self.create_link_token,
                client_name=self._client_name,
                products=["transactions"],
                required_if_supported_products=["investments"],
            )

            # Open browser
            browser_error = open_link_in_browser(link_url)
            if browser_error:
                return browser_error

            # Wait for public token by polling the shared file
            try:
                public_token = wait_for_token_from_file(timeout_seconds=timeout_seconds)
            except PublicTokenTimeoutError:
                return {
                    "status": "error",
                    "message": (
                        "Timed out waiting for Plaid Link to complete. "
                        "Please try again."
                    ),
                }

            # Exchange token and get item info
            item_data = exchange_token_and_get_item_info(
                public_token=public_token,
                exchange_public_token_fn=self.exchange_public_token,
                get_item_info_fn=cast(
                    Callable[[str], dict[str, Any]], self.get_item_info
                ),
            )
            if item_data is None:
                return {
                    "status": "error",
                    "message": "Failed to exchange public token",
                }

            access_token = item_data["access_token"]
            item_id = item_data["item_id"]
            institution_name = item_data["institution_name"]
            institution_id = item_data["institution_id"]

            # Fetch accounts for dedupe check
            accounts = self.get_accounts(access_token)

            # Build dedupe keys for new accounts: (institution_id, mask)
            new_dedupe_keys: set[tuple[str | None, str | None]] = set()
            for account in accounts:
                mask = account.get("mask")
                new_dedupe_keys.add((institution_id, mask))

            # Check for duplicates by fetching accounts live from existing items
            existing_items = db.list_plaid_items()
            duplicate_item: PlaidItem | None = None

            for existing_item in existing_items:
                try:
                    existing_accounts = self.get_accounts(existing_item.access_token)
                    for existing_account in existing_accounts:
                        existing_key = (
                            existing_item.institution_id,
                            existing_account.get("mask"),
                        )
                        if existing_key in new_dedupe_keys:
                            duplicate_item = existing_item
                            break
                    if duplicate_item:
                        break
                except PlaidClientError:
                    # Skip items with invalid/expired tokens
                    continue

            if duplicate_item:
                refreshed_item_id = duplicate_item.item_id
                sync_cursor_reset = False

                # Plaid Link may return a different item_id for a re-link.
                # Migrate local identity so item_id and access_token stay consistent.
                if item_id != duplicate_item.item_id:
                    db.migrate_plaid_item_identity(
                        old_item_id=duplicate_item.item_id,
                        new_item_id=item_id,
                        access_token=access_token,
                        institution_id=institution_id,
                        institution_name=institution_name,
                        reset_cursor=True,
                    )
                    refreshed_item_id = item_id
                    sync_cursor_reset = True
                else:
                    # Update existing item's access_token (credential refresh).
                    db.save_plaid_item(
                        item_id=duplicate_item.item_id,
                        access_token=access_token,
                        institution_id=institution_id,
                        institution_name=institution_name,
                    )

                return {
                    "status": "refreshed",
                    "item_id": refreshed_item_id,
                    "institution_name": institution_name,
                    "accounts_linked": len(accounts),
                    "sync_cursor_reset": sync_cursor_reset,
                    "message": (
                        f"Existing connection refreshed. "
                        f"Updated credentials for item {refreshed_item_id[:8]}... "
                        f"at {institution_name or 'institution'}."
                    ),
                }

            # Save new item to database (no account save needed)
            db_error = save_item_to_database(
                db=db,
                item_id=item_id,
                access_token=access_token,
                institution_id=institution_id,
                institution_name=institution_name,
            )
            if db_error:
                return db_error

            return {
                "status": "success",
                "item_id": item_id,
                "institution_name": institution_name,
                "accounts_linked": len(accounts),
                "message": build_success_message(
                    item_id=item_id,
                    institution_name=institution_name,
                ),
            }

        except KeyboardInterrupt:
            return {
                "status": "error",
                "message": "Connection cancelled by user.",
            }
        finally:
            clear_token_file()
            clear_link_token_file()

    def list_accounts(
        self,
        *,
        db: Any,  # DB type from services.db, avoiding circular import
    ) -> dict[str, Any]:
        """List all connected bank accounts from Plaid items.

        Queries the database for all Plaid items and fetches account details
        from Plaid for each item.

        Args:
            db: Database instance with list_plaid_items method

        Returns:
            Dictionary with account listing status including:
            - status: "success" or "error"
            - accounts: List of account dictionaries, each containing:
              - Account details (account_id, name, official_name, mask, subtype, type)
              - Institution information (institution_name, institution_id, item_id)
            - message: Human-readable status message
        """
        plaid_items = db.list_plaid_items()
        if not plaid_items:
            return {
                "status": "success",
                "accounts": [],
                "message": "No connected accounts found.",
            }

        all_accounts: list[dict[str, Any]] = []
        errors: list[str] = []

        for item in plaid_items:
            try:
                accounts = self.get_accounts(item.access_token)
                item_info = self.get_item_info(item.access_token)

                for account in accounts:
                    account_data: dict[str, Any] = {
                        **account,
                        "institution_name": item_info.get("institution_name")
                        or item.institution_name,
                        "institution_id": item_info.get("institution_id")
                        or item.institution_id,
                        "item_id": item.item_id,
                    }
                    account_with_institution = AccountWithInstitution.parse(
                        account_data
                    )
                    all_accounts.append(account_with_institution.model_dump())
            except Exception as e:
                errors.append(
                    f"Failed to fetch accounts for item {item.item_id}: {str(e)}"
                )

        if errors and not all_accounts:
            return {
                "status": "error",
                "accounts": [],
                "message": f"Failed to fetch accounts: {'; '.join(errors)}",
            }

        message = (
            f"Found {len(all_accounts)} account(s) across "
            f"{len(plaid_items)} institution(s)."
        )
        if errors:
            message += (
                f" Note: {len(errors)} error(s) occurred while fetching some accounts."
            )

        return {
            "status": "success",
            "accounts": all_accounts,
            "message": message,
        }

    def get_investment_transactions(
        self,
        access_token: str,
        *,
        start_date: date,
        end_date: date,
        count: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Fetch investment transactions from Plaid.

        Args:
            access_token: Plaid access token for the item
            start_date: Start date for transaction query
            end_date: End date for transaction query
            count: Number of transactions to fetch per page (default: 100, max: 500)
            offset: Offset for pagination (default: 0)

        Returns:
            Dictionary with investment_transactions, securities, and total_count
        """
        payload: dict[str, Any] = {
            "client_id": self._client_id,
            "secret": self._secret,
            "access_token": access_token,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "options": {
                "count": count,
                "offset": offset,
            },
        }
        resp = InvestmentTransactionsGetResponse.parse(
            self._post("/investments/transactions/get", payload)
        )
        return {
            "investment_transactions": [
                txn.model_dump() for txn in resp.investment_transactions
            ],
            "securities": [sec.model_dump() for sec in resp.securities],
            "total_investment_transactions": resp.total_investment_transactions,
        }

    def get_investment_holdings(
        self,
        access_token: str,
    ) -> dict[str, Any]:
        """Fetch investment holdings from Plaid.

        Args:
            access_token: Plaid access token for the item

        Returns:
            Dictionary with holdings and securities
        """
        payload: dict[str, Any] = {
            "client_id": self._client_id,
            "secret": self._secret,
            "access_token": access_token,
        }
        resp = InvestmentHoldingsGetResponse.parse(
            self._post("/investments/holdings/get", payload)
        )
        return {
            "holdings": [holding.model_dump() for holding in resp.holdings],
            "securities": [sec.model_dump() for sec in resp.securities],
        }
