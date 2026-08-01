from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

from loguru import logger

if TYPE_CHECKING:
    from penny.tools._services.categorizer import CategorizedTransaction
    from penny.tools._services.mutation_plugin import (
        DerivedTransactionPayload,
        TransactionItemPayload,
    )

from sqlalchemy import (
    Table,
    UniqueConstraint,
    case,
    create_engine,
    event,
    func,
    inspect,
    text,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult, Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, sessionmaker

from penny.adapters.db.models import (
    AccountSignConvention,
    AmazonItemDB,
    AmazonLoginProfileDB,
    AmazonOrderDB,
    Base,
    Category,
    CategoryRow,
    DerivedTransaction,
    EmailReceipt,
    EvalItem,
    EvalRun,
    Merchant,
    PendingReceiptMatch,
    PlaidItem,
    PlaidTransaction,
    SaveOutcome,
    SaveRowOutcome,
    Tag,
    TransactionCategoryEvent,
    TransactionItem,
    TransactionTag,
    normalize_merchant_name,
)

M = TypeVar("M")
CategoryMethod = Literal["llm", "manual", "taxonomy_migration"]

# Investment trades/income (buys, sells, dividends, fees) are recorded but NOT
# categorized — categorizing them spends tokens for no reporting value. They carry
# ``reporting_mode='DEFAULT_EXCLUDE'`` (set during investment sync). Regular txns
# have NULL reporting_mode; investment-account money movement is DEFAULT_INCLUDE
# and IS still categorized. This predicate selects "should be categorized".
_SKIP_CATEGORIZATION_REPORTING_MODE = "DEFAULT_EXCLUDE"


def _needs_categorization_clause() -> Any:
    """SQL clause: the row is not an investment trade to skip (NULL-safe)."""
    return (
        func.coalesce(DerivedTransaction.reporting_mode, "")
        != _SKIP_CATEGORIZATION_REPORTING_MODE
    )


_COMPACT_SCHEMA_MODELS: tuple[type[Base], ...] = (
    Merchant,
    Category,
    PlaidTransaction,
    DerivedTransaction,
    TransactionCategoryEvent,
    Tag,
    TransactionTag,
    TransactionItem,
    EmailReceipt,
    PendingReceiptMatch,
    AccountSignConvention,
)

_COMPACT_SCHEMA_NOTES: dict[str, str] = {
    "plaid_transactions": (
        "Immutable source data from Plaid. Do NOT query directly for spending analysis."
    ),
    "derived_transactions": (
        "Primary table for all spending queries and analysis. "
        "May have multiple rows per Plaid transaction (Amazon item splits). "
        "\n"
        "Refunds: a refund row is one whose refund_of_transaction_id is NOT NULL, "
        "linking it to the original charge it offsets. The FK is the truth for "
        "identifying refunds — do NOT rely on the sign of amount_cents, since some "
        "bank providers invert expense/deposit signs. "
        "refund_matched_by records who created the link ('user' or 'auto'); "
        "refund_matched_at records when. "
        "\n"
        "For spend analysis, exclude both refund rows AND the originals they "
        "offset, treating each linked pair as net-neutral:\n"
        "\n"
        "  SELECT SUM(amount_cents) FROM derived_transactions d\n"
        "  WHERE d.refund_of_transaction_id IS NULL\n"
        "    AND NOT EXISTS (\n"
        "      SELECT 1 FROM derived_transactions r\n"
        "      WHERE r.refund_of_transaction_id = d.transaction_id\n"
        "    )\n"
        "    -- plus your own filters: merchant, date range, category, etc.\n"
        "\n"
        "This is sign-agnostic. Partial refunds are over-excluded (the un-refunded "
        "portion is silently dropped) but partial refunds are rare enough that "
        "this is the right trade-off versus a per-transaction net computation. "
        "To LIST refunds explicitly (not for spend totals), query rows directly: "
        "WHERE refund_of_transaction_id IS NOT NULL. "
        "\n"
        "Amounts on derived_transactions are sign-normalized. Use SUM(amount_cents) "
        "directly in spend queries on derived_transactions; do not consult "
        "account_sign_conventions for those queries (that table is only relevant when "
        "joining plaid_transactions raw data). "
        "\n"
        "is_hidden flags rows the user has chosen to exclude from analysis. For "
        "all spend queries add `AND is_hidden = FALSE`; only drop that filter "
        "when the user explicitly asks to see hidden transactions."
    ),
    "transaction_items": (
        "Itemization table. One row per line item within a transaction. "
        "amount_cents is synthetic and proportionally allocated so that all items "
        "for a transaction sum exactly to the parent "
        "derived_transactions.amount_cents. "
        "itemization_source indicates the origin of the data "
        "('amazon_scrape', 'email_receipt', or 'manual'); "
        "nullable source_ref carries an opaque reference to the upstream record "
        "(e.g. Amazon order_id or, in future, a Gmail message_id) "
        "depending on itemization_source."
    ),
    "email_receipts": (
        "Parsed Gmail receipts; message_id is the dedup key. "
        "Sidecar table: the items it produces live in transaction_items with "
        "itemization_source='email_receipt' and source_ref=message_id. "
        "Do NOT query or return the subject or sender columns — they contain "
        "PII and must not appear in LLM responses."
    ),
    "pending_receipt_matches": (
        "Low-confidence email-receipt candidates queued for human review in the "
        "web UI. transaction_items rows are NOT written until the candidate is "
        "status='confirmed'. match_score is composite [0.0, 1.0]; lower means "
        "more uncertain."
    ),
    "categories": (
        "Filter WHERE deprecated_at IS NULL to exclude retired categories. "
        "Always include this filter when JOINing categories for analysis."
    ),
    "account_sign_conventions": (
        "Per-account lookup for expense sign convention. "
        "sign_convention is either 'expense_positive' (expenses are positive "
        "amount_cents, the Plaid default) or 'expense_negative' (expenses are "
        "negative amount_cents, used by some institutions). "
        "account_id matches plaid_transactions.account_id. "
        "Rows are normally populated automatically by the seeding pipeline "
        "(provenance='seeded'); manual overrides have provenance='manual'. "
        "Missing rows should be treated as 'expense_positive'. "
        "To normalize plaid_transactions.amount_cents for spend analysis: "
        "LEFT JOIN account_sign_conventions a ON a.account_id = pt.account_id, "
        "then use CASE WHEN COALESCE(a.sign_convention, 'expense_positive') = "
        "'expense_negative' THEN -pt.amount_cents ELSE pt.amount_cents END."
    ),
}


def _enable_sqlite_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
    """Enable SQLite FK enforcement so RESTRICT/CASCADE behave like Postgres."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def _enable_sqlite_wal(dbapi_connection: Any, _record: Any) -> None:
    """WAL journal mode: the server, daemon, and MCP processes share one file.

    WAL lets readers proceed during a writer's transaction (and vice versa),
    which is what makes the multi-process single-player topology workable.
    A no-op on in-memory databases.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def _stored_token(access_token: str) -> str:
    """Plaid access tokens are encrypted at rest when the key is configured.

    Unconfigured keeps plaintext (single-player: the local DB is the user's
    own; encryption at rest is opt-in), and migration 017 encrypts the backlog
    once the key exists. Already-encrypted values pass through, so re-saving
    never double-encrypts. Decryption happens only at the Plaid wire seam
    (PlaidClient._with_decrypted_token).
    """
    from penny.security.token_cipher import encrypt_token_at_rest, is_encrypted

    if is_encrypted(access_token):
        return access_token
    return encrypt_token_at_rest(access_token)


class DB:
    """Database service layer providing ORM models and helper methods."""

    def __init__(
        self,
        url: str,
        *,
        enforce_sqlite_fks: bool = False,
    ) -> None:
        """Initialize database connection.

        Args:
            url: Database URL (e.g., "sqlite:///penny.db")
            enforce_sqlite_fks: When True and the URL is SQLite, enable
                ``PRAGMA foreign_keys=ON`` so RESTRICT/CASCADE behave like
                Postgres. Off by default — pre-existing tests rely on the
                permissive default.
        """
        engine_kwargs: dict[str, Any] = {"echo": False}
        if not url.startswith("sqlite"):
            # Keep long-lived CLI/ACP sessions resilient to dropped DB connections.
            engine_kwargs.update(
                {
                    "pool_pre_ping": True,
                    "pool_recycle": 300,
                }
            )
        self._engine = create_engine(url, **engine_kwargs)
        if url.startswith("sqlite"):
            event.listen(self._engine, "connect", _enable_sqlite_wal)
            if enforce_sqlite_fks:
                event.listen(self._engine, "connect", _enable_sqlite_foreign_keys)
        self._session_factory = sessionmaker(bind=self._engine, class_=Session)

    @property
    def session_factory(self) -> sessionmaker:
        """The engine's session factory — the seam app stores bind through."""
        return self._session_factory

    @property
    def dialect(self) -> str:
        """The engine's dialect name: ``"sqlite"`` | ``"postgresql"``."""
        return self._engine.dialect.name

    @property
    def engine(self) -> Engine:
        """The underlying engine — for schema-level checks (bootstrap, guards)."""
        return self._engine

    def create_schema(self) -> None:
        """Build the schema from the models. SQLite (ephemeral dev/test) ONLY.

        On Postgres the schema is owned by alembic — run ``penny migrate``
        (``upgrade head``). ``create_all`` can't ALTER / backfill / enable RLS,
        so on a durable Postgres DB it would silently half-migrate and collide
        with the migration chain (the phase-3 cutover root cause). Refused here.
        """
        if self._engine.dialect.name != "sqlite":
            raise RuntimeError(
                "create_schema()/create_all is SQLite-only; the Postgres schema "
                "is alembic-owned. Run `penny migrate` (alembic upgrade head)."
            )
        Base.metadata.create_all(self._engine)

    def dispose(self) -> None:
        """Close every pooled connection.

        On a WAL-mode SQLite file, closing the last connection checkpoints the
        ``-wal`` sidecar back into the main file — required before treating the
        file's raw bytes as the complete database (e.g. the eval fixture).
        """
        self._engine.dispose()

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Context manager for database sessions."""
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def execute_raw_sql(self, query: str) -> CursorResult[Any]:
        """Execute raw SQL query and return cursor result.

        Returns CursorResult instead of generic Result to access:
        - returns_rows: Whether query returns rows
        - rowcount: Number of rows affected

        Args:
            query: Raw SQL query string

        Returns:
            CursorResult with proper typing for attribute access
        """
        with self.session() as session:  # type: Session
            return cast(CursorResult[Any], session.execute(text(query)))

    def run_sql(
        self,
        sql: str,
        *,
        model: type[M],
        pk_column: str,
    ) -> list[M]:
        """Execute raw SQL and return ORM model instances.

        Args:
            sql: Raw SQL SELECT query
            model: SQLAlchemy model class to return
            pk_column: Name of primary key column in SQL result

        Returns:
            List of ORM model instances in the order returned by SQL
        """
        with self.session() as session:  # type: Session
            result = session.execute(text(sql))
            rows = result.fetchall()

            if not rows:
                return []

            # Get column index for primary key
            keys = list(result.keys())
            pk_index = keys.index(pk_column)

            # Extract primary key values
            pk_values = [row[pk_index] for row in rows]

            # Query ORM models by primary keys
            pk_attr = getattr(model, pk_column)
            orm_instances = session.query(model).filter(pk_attr.in_(pk_values)).all()

            # Expunge all instances before returning
            for instance in orm_instances:
                session.expunge(instance)

            # Create mapping for quick lookup
            instance_map = {getattr(inst, pk_column): inst for inst in orm_instances}

            # Return in SQL result order
            return [
                instance_map[pk_val] for pk_val in pk_values if pk_val in instance_map
            ]

    def fetch_transactions_by_ids_preserving_order(
        self,
        ids: list[int],
    ) -> list[DerivedTransaction]:
        """Fetch derived transactions by IDs preserving input order.

        Args:
            ids: List of transaction IDs

        Returns:
            List of DerivedTransaction instances in the same order as input IDs
        """
        if not ids:
            return []

        with self.session() as session:  # type: Session
            # Use CASE WHEN to preserve order
            order_case = case(
                {id_val: idx for idx, id_val in enumerate(ids)},
                value=DerivedTransaction.transaction_id,
            )
            transactions = (
                session.query(DerivedTransaction)
                .filter(DerivedTransaction.transaction_id.in_(ids))
                .order_by(order_case)
                .all()
            )

            # Expunge all transactions before returning
            for txn in transactions:
                session.expunge(txn)
            # Create mapping and return in input order
            txn_map = {txn.transaction_id: txn for txn in transactions}
            return [txn_map[tid] for tid in ids if tid in txn_map]

    def get_category_id_by_key(self, key: str) -> int | None:
        """Get active category ID by key.

        Deprecated categories (`deprecated_at IS NOT NULL`) are excluded —
        a deprecated key resolves to None so it cannot accept new assignments.

        Args:
            key: Category key

        Returns:
            Category ID or None if not found
        """
        with self.session() as session:  # type: Session
            category = (
                session.query(Category)
                .filter(Category.key == key, Category.deprecated_at.is_(None))
                .first()
            )
            return category.category_id if category else None

    def get_category_ids_by_keys(self, keys: list[str]) -> dict[str, int]:
        """Get active category IDs for multiple keys in a single query.

        Deprecated categories are excluded; missing or deprecated keys are
        omitted from the returned dict.

        Args:
            keys: List of category keys

        Returns:
            Dict mapping category key to category_id (missing keys omitted)
        """
        if not keys:
            return {}

        with self.session() as session:  # type: Session
            categories = (
                session.query(Category)
                .filter(Category.key.in_(keys), Category.deprecated_at.is_(None))
                .all()
            )
            return {cat.key: cat.category_id for cat in categories}

    def _resolve_category_keys(
        self, session: Session, category_ids: set[int]
    ) -> dict[int, str]:
        """Resolve category IDs to keys in one query."""
        if not category_ids:
            return {}

        categories = (
            session.query(Category).filter(Category.category_id.in_(category_ids)).all()
        )
        return {category.category_id: category.key for category in categories}

    def _insert_category_event(
        self,
        session: Session,
        *,
        transaction_id: int,
        from_category_id: int | None,
        to_category_id: int,
        from_category_key: str | None,
        to_category_key: str,
        method: CategoryMethod,
        model: str | None,
        reason: str | None,
        created_at: datetime,
    ) -> None:
        """Insert a category change event row.

        ``reason`` is routed to the column that matches ``method``:
        - ``llm`` (an agent/LLM categorization decision) -> ``categorization_reasoning``
          (why this category was chosen).
        - ``manual`` / ``taxonomy_migration`` (a change to an existing category) ->
          ``recategorization_reason`` (why it changed).
        """
        if method == "llm":
            categorization_reasoning = reason
            recategorization_reason = None
        else:
            recategorization_reason = reason
            categorization_reasoning = None
        session.add(
            TransactionCategoryEvent(
                transaction_id=transaction_id,
                from_category_id=from_category_id,
                to_category_id=to_category_id,
                from_category_key=from_category_key,
                to_category_key=to_category_key,
                method=method,
                model=model,
                recategorization_reason=recategorization_reason,
                categorization_reasoning=categorization_reasoning,
                created_at=created_at,
            )
        )

    def _apply_category_updates(
        self,
        session: Session,
        *,
        updates: dict[int, int],
        method: CategoryMethod,
        reason: str | None,
        model: str | None = None,
        preserve_model: bool = False,
        is_verified: bool | None = None,
    ) -> int:
        """Atomically update current category fields and append history events.

        ``is_verified`` controls the verified flag: ``True``/``False`` sets the
        column to that value; ``None`` (default) leaves the existing value
        untouched.
        """
        if not updates:
            return 0

        now = datetime.now()
        txns = (
            session.query(DerivedTransaction)
            .filter(DerivedTransaction.transaction_id.in_(updates.keys()))
            .all()
        )
        if not txns:
            return 0

        category_ids: set[int] = set(updates.values())
        category_ids.update(
            txn.category_id for txn in txns if txn.category_id is not None
        )
        key_by_id = self._resolve_category_keys(session, category_ids)

        updated_count = 0
        for txn in txns:
            new_category_id = updates.get(txn.transaction_id)
            if new_category_id is None:
                continue

            to_category_key = key_by_id.get(new_category_id)
            if to_category_key is None:
                raise ValueError(f"Category with ID {new_category_id} does not exist")

            previous_category_id = txn.category_id
            previous_category_key = (
                key_by_id.get(previous_category_id)
                if previous_category_id is not None
                else None
            )

            txn.category_id = new_category_id
            txn.category_method = method
            txn.category_assigned_at = now
            if not preserve_model:
                txn.category_model = model
            if is_verified is not None:
                txn.is_verified = is_verified
            txn.updated_at = now

            self._insert_category_event(
                session,
                transaction_id=txn.transaction_id,
                from_category_id=previous_category_id,
                to_category_id=new_category_id,
                from_category_key=previous_category_key,
                to_category_key=to_category_key,
                method=method,
                model=None if preserve_model else model,
                reason=reason,
                created_at=now,
            )
            updated_count += 1

        return updated_count

    def find_merchant_by_normalized_name(self, normalized_name: str) -> Merchant | None:
        """Find merchant by normalized name.

        Args:
            normalized_name: Normalized merchant name

        Returns:
            Merchant instance or None if not found
        """
        with self.session() as session:  # type: Session
            merchant = (
                session.query(Merchant)
                .filter(Merchant.normalized_name == normalized_name)
                .first()
            )
            if merchant:
                session.expunge(merchant)
            return merchant

    def create_merchant(
        self,
        *,
        normalized_name: str,
        display_name: str | None,
    ) -> Merchant:
        """Create a new merchant.

        Args:
            normalized_name: Normalized merchant name (must be unique)
            display_name: Display name for merchant

        Returns:
            Created Merchant instance
        """
        with self.session() as session:  # type: Session
            merchant = Merchant(
                normalized_name=normalized_name, display_name=display_name
            )
            session.add(merchant)
            session.flush()
            session.refresh(merchant)
            session.expunge(merchant)
            return merchant

    def get_or_create_merchant_id(
        self,
        *,
        normalized_name: str,
        display_name: str | None,
        source_channel: str | None = None,
        counterparty: str | None = None,
    ) -> int:
        """Resolve a normalized merchant identity to a merchant_id.

        Looks up the merchant by ``normalized_name`` (the stable identity key);
        creates it with the supplied metadata if absent. Takes primitives rather
        than a NormalizedMerchant so the DB layer stays decoupled from the
        normalizer package. Metadata is set at creation; existing rows are left
        as-is (the identity is the normalized_name).
        """
        with self.session() as session:  # type: Session
            merchant = (
                session.query(Merchant)
                .filter(Merchant.normalized_name == normalized_name)
                .first()
            )
            if merchant is None:
                merchant = Merchant(
                    normalized_name=normalized_name,
                    display_name=display_name,
                    source_channel=source_channel,
                    counterparty=counterparty,
                )
                session.add(merchant)
                session.flush()
            return merchant.merchant_id

    def get_transaction_by_external(
        self,
        *,
        external_id: str,
        source: str = "PLAID",
    ) -> DerivedTransaction | None:
        """Get derived transaction by external ID.

        Args:
            external_id: External transaction ID
            source: Ignored (kept for backward compatibility)

        Returns:
            DerivedTransaction instance or None if not found
        """
        with self.session() as session:  # type: Session
            transaction = (
                session.query(DerivedTransaction)
                .filter(DerivedTransaction.external_id == external_id)
                .first()
            )
            if transaction:
                session.expunge(transaction)
            return transaction

    def insert_transaction(self, data: dict[str, Any]) -> DerivedTransaction:
        """Insert a new derived transaction (legacy wrapper).

        DEPRECATED: Use insert_derived_transaction() instead.
        This method creates both a PlaidTransaction and DerivedTransaction
        for backward compatibility.

        Args:
            data: Transaction data dictionary with fields:
                - external_id, source, account_id, posted_at, amount_cents, currency
                - merchant_descriptor (optional), merchant_id (optional)
                - category_id (optional), institution (optional)

        Returns:
            Created DerivedTransaction instance
        """
        # First create PlaidTransaction
        plaid_txn = self.upsert_plaid_transaction(
            external_id=data["external_id"],
            source=data.get("source", "PLAID"),
            account_id=data["account_id"],
            posted_at=data["posted_at"],
            amount_cents=data["amount_cents"],
            currency=data["currency"],
            merchant_descriptor=data.get("merchant_descriptor"),
            institution=data.get("institution"),
            original_descriptor=data.get("original_descriptor"),
            raw_name=data.get("raw_name"),
            counterparties=data.get("counterparties"),
            personal_finance_category=data.get("personal_finance_category"),
        )

        # Then create DerivedTransaction
        derived_data = {
            "plaid_transaction_id": plaid_txn.plaid_transaction_id,
            "external_id": data["external_id"],
            "amount_cents": data["amount_cents"],
            "posted_at": data["posted_at"],
            "merchant_descriptor": data.get("merchant_descriptor"),
            "merchant_id": data.get("merchant_id"),
            "category_id": data.get("category_id"),
            "category_model": data.get("category_model"),
            "category_method": data.get("category_method"),
            "category_assigned_at": data.get("category_assigned_at"),
            "web_search_summary": data.get("web_search_summary"),
            "is_verified": data.get("is_verified", False),
        }
        return self.insert_derived_transaction(derived_data)

    def update_transaction_mutable(
        self,
        transaction_id: int,
        data: dict[str, Any],
    ) -> DerivedTransaction:
        """Update mutable fields of a derived transaction (legacy wrapper).

        DEPRECATED: Use update_derived_mutable() instead.

        Only updates if transaction is not verified (is_verified=False).

        Args:
            transaction_id: Transaction ID to update
            data: Dictionary of fields to update

        Returns:
            Updated DerivedTransaction instance

        Raises:
            ValueError: If transaction is verified and cannot be updated
        """
        return self.update_derived_mutable(transaction_id, data)

    def recategorize_merchant(
        self,
        merchant_id: int,
        category_id: int,
        reason: str | None = None,
    ) -> int:
        """Recategorize all unverified derived transactions for a merchant.

        Args:
            merchant_id: Merchant ID
            category_id: New category ID
            reason: Natural-language reason for the change (recorded on each
                event's ``recategorization_reason``). Falls back to a marker
                string when not supplied.

        Returns:
            Number of transactions updated
        """
        with self.session() as session:  # type: Session
            txns = (
                session.query(DerivedTransaction)
                .filter(
                    DerivedTransaction.merchant_id == merchant_id,
                    ~DerivedTransaction.is_verified,
                )
                .all()
            )
            updates = {txn.transaction_id: category_id for txn in txns}
            return self._apply_category_updates(
                session,
                updates=updates,
                method="manual",
                reason=reason or "recategorize_merchant",
                preserve_model=True,
            )

    def recategorize_transaction(
        self,
        transaction_id: int,
        category_id: int,
        *,
        reason: str | None = None,
        verify: bool = True,
    ) -> dict[str, Any]:
        """Recategorize a single derived transaction.

        Sets ``category_method='manual'`` and ``category_model=NULL``. When
        ``verify`` is True the row is marked ``is_verified=True`` (protecting it
        from future bulk ``recategorize_merchant`` runs); otherwise the existing
        verified flag is left untouched. A ``transaction_category_events`` row is
        appended in the same transaction.

        Returns a dict with ``updated`` (bool) and ``event_id`` (int | None).

        Raises:
            ValueError: If the transaction does not exist.
        """
        with self.session() as session:  # type: Session
            txn = session.get(DerivedTransaction, transaction_id)
            if txn is None:
                raise ValueError(f"Transaction {transaction_id} does not exist")

            updated = self._apply_category_updates(
                session,
                updates={transaction_id: category_id},
                method="manual",
                reason=reason,
                is_verified=True if verify else None,
            )
            session.flush()

            event = (
                session.query(TransactionCategoryEvent)
                .filter(TransactionCategoryEvent.transaction_id == transaction_id)
                .order_by(TransactionCategoryEvent.event_id.desc())
                .first()
            )
            return {
                "updated": updated > 0,
                "event_id": event.event_id if event is not None else None,
            }

    def upsert_tag(self, name: str, description: str | None = None) -> Tag:
        """Insert or update a tag.

        Args:
            name: Tag name (unique within the household)
            description: Tag description

        Returns:
            Tag instance
        """
        with self.session() as session:  # type: Session
            tag = session.query(Tag).filter(Tag.name == name).first()
            if tag is None:
                tag = Tag(name=name, description=description)
                session.add(tag)
            else:
                if description is not None:
                    tag.description = description
            session.flush()
            session.refresh(tag)
            session.expunge(tag)
            return tag

    def attach_tags(self, transaction_ids: list[int], tag_ids: list[int]) -> int:
        """Attach tags to transactions (bulk insert, skip duplicates).

        Args:
            transaction_ids: List of transaction IDs
            tag_ids: List of tag IDs

        Returns:
            Number of tag attachments created
        """
        if not transaction_ids or not tag_ids:
            return 0

        with self.session() as session:  # type: Session
            # Check existing relationships
            existing = (
                session.query(TransactionTag)
                .filter(
                    TransactionTag.transaction_id.in_(transaction_ids),
                    TransactionTag.tag_id.in_(tag_ids),
                )
                .all()
            )
            existing_set = {(rel.transaction_id, rel.tag_id) for rel in existing}

            # Insert new relationships
            new_count = 0
            for transaction_id in transaction_ids:
                for tag_id in tag_ids:
                    if (transaction_id, tag_id) not in existing_set:
                        rel = TransactionTag(
                            transaction_id=transaction_id, tag_id=tag_id
                        )
                        session.add(rel)
                        new_count += 1

            return new_count

    def set_transactions_visibility(
        self, transaction_ids: list[int], visible: bool
    ) -> int:
        """Set is_hidden on the given derived transactions.

        Args:
            transaction_ids: derived_transactions.transaction_id values.
            visible: True to show (unhide), False to hide.

        Returns:
            Number of transactions whose flag was updated (matched rows).
        """
        if not transaction_ids:
            return 0

        with self.session() as session:  # type: Session
            rows = (
                session.query(DerivedTransaction)
                .filter(DerivedTransaction.transaction_id.in_(transaction_ids))
                .all()
            )
            for row in rows:
                row.is_hidden = not visible
            return len(rows)

    def delete_transactions_by_external_ids(
        self,
        external_ids: list[str],
        source: str = "PLAID",
    ) -> int:
        """Delete derived transactions by their external IDs (legacy wrapper).

        DEPRECATED: Use delete_plaid_transactions_by_external_ids() for full
        cascade delete, or delete_derived_by_plaid_ids() for derived-only.

        Only deletes unverified transactions to respect immutability guarantees.

        Args:
            external_ids: List of external transaction IDs (e.g., Plaid transaction_id)
            source: Source identifier (default: "PLAID") - ignored

        Returns:
            Number of transactions deleted
        """
        if not external_ids:
            return 0

        with self.session() as session:  # type: Session
            result = (
                session.query(DerivedTransaction)
                .filter(
                    DerivedTransaction.external_id.in_(external_ids),
                    ~DerivedTransaction.is_verified,
                )
                .delete(synchronize_session=False)
            )
            return result

    def save_transactions(
        self,
        category_lookup: Callable[[str], int | None],
        txns: Iterable[CategorizedTransaction],
    ) -> SaveOutcome:
        """Save categorized transactions to the database.

        Args:
            category_lookup: Function that takes a category key and returns category ID
            txns: Iterable of categorized transactions to save

        Returns:
            SaveOutcome with details about the save operation
        """
        inserted_count = 0
        updated_count = 0
        skipped_verified_count = 0
        skipped_duplicate_count = 0
        rows: list[SaveRowOutcome] = []

        for cat_txn in txns:
            txn = cat_txn.txn

            # Determine category key (prefer revised if present)
            category_key = (
                cat_txn.revised_category_key
                if cat_txn.revised_category_key
                else cat_txn.category_key
            )
            category_id = category_lookup(category_key) if category_key else None
            llm_research_summary = (
                cat_txn.merchant_summary if cat_txn.used_web_search else None
            )
            if llm_research_summary is not None and not llm_research_summary.strip():
                llm_research_summary = None

            # Extract transaction data
            # Map from Transaction TypedDict to database fields
            merchant_descriptor = txn.get("merchant_name") or txn.get("name")
            external_id = txn.get("transaction_id") or ""
            source = "PLAID"  # Default, should come from ingest tool context

            # Parse date
            posted_at_str = txn.get("date", "")
            try:
                posted_at = datetime.strptime(posted_at_str, "%Y-%m-%d").date()
            except (ValueError, TypeError):
                rows.append(
                    SaveRowOutcome(
                        external_id=external_id,
                        source=source,
                        action="skipped-duplicate",
                        reason=f"Invalid date: {posted_at_str}",
                    )
                )
                skipped_duplicate_count += 1
                continue

            # Convert amount to cents
            amount = txn.get("amount", 0.0)
            amount_cents = int(amount * 100)

            # Currency
            currency = txn.get("iso_currency_code") or "USD"

            # Check if transaction exists
            existing = self.get_transaction_by_external(
                external_id=external_id, source=source
            )

            if existing:
                is_verified = existing.is_verified
                existing_id = existing.transaction_id
                if is_verified:
                    rows.append(
                        SaveRowOutcome(
                            external_id=external_id,
                            source=source,
                            action="skipped-verified",
                            transaction_id=existing_id,
                            reason="Transaction is verified and cannot be updated",
                        )
                    )
                    skipped_verified_count += 1
                    continue

                # Update existing unverified transaction
                update_data: dict[str, Any] = {
                    "category_id": category_id,
                    "category_method": "llm",
                    "category_model": cat_txn.category_model,
                    "category_assigned_at": datetime.now(),
                    "category_reason": "save_transactions",
                    "merchant_descriptor": merchant_descriptor,
                    "amount_cents": amount_cents,
                    "web_search_summary": llm_research_summary,
                }
                try:
                    updated_txn = self.update_transaction_mutable(
                        existing_id, update_data
                    )
                    rows.append(
                        SaveRowOutcome(
                            external_id=external_id,
                            source=source,
                            action="updated",
                            transaction_id=updated_txn.transaction_id,
                        )
                    )
                    updated_count += 1
                except ValueError as e:
                    rows.append(
                        SaveRowOutcome(
                            external_id=external_id,
                            source=source,
                            action="skipped-verified",
                            transaction_id=existing_id,
                            reason=str(e),
                        )
                    )
                    skipped_verified_count += 1
            else:
                # Insert new transaction
                insert_data: dict[str, Any] = {
                    "external_id": external_id,
                    "source": source,
                    "account_id": txn.get("account_id", ""),
                    "posted_at": posted_at,
                    "amount_cents": amount_cents,
                    "currency": currency,
                    "merchant_descriptor": merchant_descriptor,
                    "original_descriptor": txn.get("original_descriptor"),
                    "category_id": category_id,
                    "category_method": "llm" if category_id is not None else None,
                    "category_model": (
                        cat_txn.category_model if category_id is not None else None
                    ),
                    "category_assigned_at": (
                        datetime.now() if category_id is not None else None
                    ),
                    "category_reason": (
                        "save_transactions" if category_id is not None else None
                    ),
                    "web_search_summary": llm_research_summary,
                    "institution": None,  # Should come from ingest tool context
                }
                new_txn = self.insert_transaction(insert_data)
                rows.append(
                    SaveRowOutcome(
                        external_id=external_id,
                        source=source,
                        action="inserted",
                        transaction_id=new_txn.transaction_id,
                    )
                )
                inserted_count += 1

        return SaveOutcome(
            inserted=inserted_count,
            updated=updated_count,
            skipped_verified=skipped_verified_count,
            skipped_duplicate=skipped_duplicate_count,
            rows=rows,
        )

    def compact_schema_hint(self) -> dict[str, Any]:
        """Return compact schema metadata for LLM prompts.

        Returns:
            Dictionary with table names, column names, types, and relationships
        """
        tables: dict[str, dict[str, Any]] = {}
        for model in _COMPACT_SCHEMA_MODELS:
            table_data: dict[str, Any] = {
                "columns": self._build_compact_schema_columns(model=model),
                "relationships": self._build_compact_schema_relationships(model=model),
            }
            constraints = self._build_compact_schema_constraints(model=model)
            if constraints:
                table_data["constraints"] = constraints

            notes = _COMPACT_SCHEMA_NOTES.get(model.__tablename__)
            if notes is not None:
                table_data["notes"] = notes

            tables[model.__tablename__] = table_data

        return {"tables": tables}

    def _build_compact_schema_columns(self, *, model: type[Base]) -> dict[str, str]:
        """Build compact, human-readable column type metadata for one model."""
        mapper = inspect(model)
        columns: dict[str, str] = {}
        for column in mapper.columns:
            column_parts = [str(column.type).upper()]
            if column.primary_key:
                column_parts.append("PRIMARY KEY")
            if column.unique:
                column_parts.append("UNIQUE")
            if column.foreign_keys:
                column_parts.append("FOREIGN KEY")
            columns[column.name] = " ".join(column_parts)
        return columns

    def _build_compact_schema_constraints(self, *, model: type[Base]) -> list[str]:
        """Build compact table-level constraints for one model."""
        table = cast(Table, model.__table__)
        constraints: list[str] = []
        for constraint in table.constraints:
            if not isinstance(constraint, UniqueConstraint):
                continue
            if len(constraint.columns) < 2:
                continue
            column_names = ", ".join(column.name for column in constraint.columns)
            constraints.append(f"UNIQUE({column_names})")
        return constraints

    def _build_compact_schema_relationships(self, *, model: type[Base]) -> list[str]:
        """Build related table names using ORM relationships and foreign keys."""
        mapper = inspect(model)
        table = cast(Table, model.__table__)
        related_table_names = {
            str(rel.entity.class_.__tablename__) for rel in mapper.relationships
        }
        for foreign_key in table.foreign_keys:
            related_table_names.add(foreign_key.target_fullname.split(".", 1)[0])
        return sorted(related_table_names)

    def fetch_categories(
        self, *, include_deprecated: bool = False
    ) -> list[CategoryRow]:
        """Fetch categories as CategoryRow TypedDicts.

        Args:
            include_deprecated: If True, include deprecated categories.
                Defaults to False (only active categories).

        Returns:
            List of CategoryRow dictionaries
        """
        with self.session() as session:  # type: Session
            query = session.query(Category)
            if not include_deprecated:
                query = query.filter(Category.deprecated_at.is_(None))
            categories = query.all()

            # Build parent_key lookup
            id_to_key: dict[int, str] = {cat.category_id: cat.key for cat in categories}

            rows: list[CategoryRow] = []
            for cat in categories:
                parent_key = None
                if cat.parent_id is not None:
                    parent_key = id_to_key.get(cat.parent_id)

                rows.append(
                    CategoryRow(
                        category_id=cat.category_id,
                        parent_id=cat.parent_id,
                        key=cat.key,
                        name=cat.name,
                        description=cat.description,
                        parent_key=parent_key,
                        deprecated_at=cat.deprecated_at,
                    )
                )

            return rows

    def replace_categories_rows(self, rows: Sequence[CategoryRow]) -> None:
        """Replace categories with pre-built rows (ids and parent ids already resolved).

        Args:
            rows: Sequence of CategoryRow dictionaries with resolved IDs
        """
        with self.session() as session:  # type: Session
            # Delete the context household's existing categories
            session.query(Category).delete()

            # Insert new categories
            for row in rows:
                category = Category(
                    category_id=row["category_id"],
                    parent_id=row["parent_id"],
                    key=row["key"],
                    name=row["name"],
                    description=row.get("description"),
                    rules=None,  # Rules not in CategoryRow, set to None
                )
                session.add(category)

            session.flush()

    def save_plaid_item(
        self,
        *,
        item_id: str,
        access_token: str,
        institution_id: str | None = None,
        institution_name: str | None = None,
    ) -> PlaidItem:
        """Save or update a Plaid item.

        Args:
            item_id: Plaid item ID (primary key)
            access_token: Plaid access token
            institution_id: Optional institution ID
            institution_name: Optional institution name

        Returns:
            Created or updated PlaidItem instance
        """
        access_token = _stored_token(access_token)
        with self.session() as session:  # type: Session
            item = session.query(PlaidItem).filter_by(item_id=item_id).first()
            if item is None:
                item = PlaidItem(
                    item_id=item_id,
                    access_token=access_token,
                    institution_id=institution_id,
                    institution_name=institution_name,
                )
                session.add(item)
            else:
                item.access_token = access_token
                item.institution_id = institution_id
                item.institution_name = institution_name
                item.updated_at = datetime.now()
            session.flush()
            session.refresh(item)
            session.expunge(item)
            return item

    def get_plaid_item(self, item_id: str) -> PlaidItem | None:
        """Retrieve a Plaid item by item_id.

        Args:
            item_id: Plaid item ID

        Returns:
            PlaidItem instance or None if not found
        """
        with self.session() as session:  # type: Session
            item = session.query(PlaidItem).filter_by(item_id=item_id).first()
            if item:
                session.expunge(item)
            return item

    def get_sync_cursor(self, item_id: str) -> str | None:
        """Get the sync cursor for a Plaid item.

        Args:
            item_id: Plaid item ID

        Returns:
            Sync cursor string or None if not set
        """
        with self.session() as session:  # type: Session
            item = session.query(PlaidItem).filter_by(item_id=item_id).first()
            return item.sync_cursor if item else None

    def set_sync_cursor(self, item_id: str, cursor: str) -> None:
        """Set the sync cursor for a Plaid item.

        Args:
            item_id: Plaid item ID
            cursor: Sync cursor string from Plaid
        """
        with self.session() as session:  # type: Session
            item = session.query(PlaidItem).filter_by(item_id=item_id).first()
            if item:
                item.sync_cursor = cursor
                item.updated_at = datetime.now()

    def set_investments_watermark(self, item_id: str, watermark_date: date) -> None:
        """Set the investments watermark for a Plaid item.

        Args:
            item_id: Plaid item ID
            watermark_date: Date through which investments have been synced
        """
        with self.session() as session:  # type: Session
            item = session.query(PlaidItem).filter_by(item_id=item_id).first()
            if item:
                item.investments_synced_through = watermark_date
                item.updated_at = datetime.now()

    def insert_plaid_item(
        self,
        item_id: str,
        access_token: str,
        institution_id: str | None = None,
        institution_name: str | None = None,
    ) -> PlaidItem:
        """Insert a new Plaid item.

        Args:
            item_id: Unique identifier for the Plaid item
            access_token: Access token for the Plaid item
            institution_id: Optional Plaid institution ID
            institution_name: Optional name of the institution

        Returns:
            Created PlaidItem instance
        """
        access_token = _stored_token(access_token)
        with self.session() as session:  # type: Session
            plaid_item = PlaidItem(
                item_id=item_id,
                access_token=access_token,
                institution_id=institution_id,
                institution_name=institution_name,
            )
            session.add(plaid_item)
            session.flush()
            session.refresh(plaid_item)
            session.expunge(plaid_item)
            return plaid_item

    def list_plaid_items(self) -> list[PlaidItem]:
        """List all Plaid items.

        Returns:
            List of all PlaidItem instances
        """
        with self.session() as session:  # type: Session
            items = session.query(PlaidItem).all()
            for item in items:
                session.expunge(item)
            return items

    def has_plaid_items(self) -> bool:
        """True when at least one Plaid item is linked."""
        with self.session() as session:  # type: Session
            return session.query(PlaidItem.item_id).first() is not None

    def migrate_plaid_item_identity(
        self,
        *,
        old_item_id: str,
        new_item_id: str,
        access_token: str,
        institution_id: str | None = None,
        institution_name: str | None = None,
        reset_cursor: bool = True,
    ) -> PlaidItem:
        """Migrate a Plaid item ID while preserving linked transactions.

        This is used when Plaid Link returns a new item_id for what is logically
        the same institution connection. All existing plaid_transactions rows
        referencing old_item_id are reassigned to new_item_id.

        Args:
            old_item_id: Existing local item ID to migrate from
            new_item_id: New Plaid item ID to migrate to
            access_token: Access token for the new Plaid item
            institution_id: Optional institution ID
            institution_name: Optional institution name
            reset_cursor: If True, clear sync cursor on migrated item

        Returns:
            Migrated PlaidItem instance

        Raises:
            ValueError: If old_item_id does not exist
        """
        access_token = _stored_token(access_token)
        with self.session() as session:  # type: Session
            old_item = session.query(PlaidItem).filter_by(item_id=old_item_id).first()
            if old_item is None:
                raise ValueError(f"Plaid item {old_item_id} not found")

            # Refresh in place when item IDs already match.
            if old_item_id == new_item_id:
                old_item.access_token = access_token
                old_item.institution_id = institution_id
                old_item.institution_name = institution_name
                if reset_cursor:
                    old_item.sync_cursor = None
                old_item.updated_at = datetime.now()
                session.flush()
                session.refresh(old_item)
                session.expunge(old_item)
                return old_item

            target_item = (
                session.query(PlaidItem).filter_by(item_id=new_item_id).first()
            )
            if target_item is None:
                target_item = PlaidItem(
                    item_id=new_item_id,
                    access_token=access_token,
                    institution_id=institution_id,
                    institution_name=institution_name,
                    sync_cursor=None if reset_cursor else old_item.sync_cursor,
                )
                session.add(target_item)
                session.flush()
            else:
                target_item.access_token = access_token
                target_item.institution_id = institution_id
                target_item.institution_name = institution_name
                if reset_cursor:
                    target_item.sync_cursor = None
                target_item.updated_at = datetime.now()

            # Repoint source transactions to the new item ID before deleting old item.
            (
                session.query(PlaidTransaction)
                .filter(PlaidTransaction.item_id == old_item_id)
                .update(
                    {PlaidTransaction.item_id: new_item_id}, synchronize_session=False
                )
            )

            session.delete(old_item)
            session.flush()
            session.refresh(target_item)
            session.expunge(target_item)
            return target_item

    # Plaid Transactions methods

    def min_plaid_transaction_date(self) -> date | None:
        """Return the earliest ``posted_at`` across all plaid_transactions."""
        with self.session() as session:  # type: Session
            result = session.query(func.min(PlaidTransaction.posted_at)).scalar()
            if result is None:
                return None
            return cast(date, result)

    def max_plaid_transaction_created_at(self) -> datetime | None:
        """Return the newest ``created_at`` across all plaid_transactions."""
        with self.session() as session:  # type: Session
            result = session.query(func.max(PlaidTransaction.created_at)).scalar()
            if result is None:
                return None
            return cast(datetime, result)

    def amazon_order_date_bounds(self) -> tuple[date, date] | None:
        """Return ``(min, max)`` of ``amazon_orders.order_date``.

        Returns ``None`` when there are no scraped Amazon orders.
        """
        with self.session() as session:  # type: Session
            row = session.query(
                func.min(AmazonOrderDB.order_date),
                func.max(AmazonOrderDB.order_date),
            ).one()
            lo, hi = row
            if lo is None or hi is None:
                return None
            return (cast(date, lo), cast(date, hi))

    def list_plaid_transactions_in_date_range(
        self, *, start: date, end: date
    ) -> list[PlaidTransaction]:
        """List Plaid transactions with ``start <= posted_at <= end``.

        Ordered by ``posted_at`` for deterministic downstream matching.
        Rows are detached so callers can use them outside the session.
        """
        with self.session() as session:  # type: Session
            txns = (
                session.query(PlaidTransaction)
                .filter(
                    PlaidTransaction.posted_at >= start,
                    PlaidTransaction.posted_at <= end,
                )
                .order_by(PlaidTransaction.posted_at.asc())
                .all()
            )
            for txn in txns:
                session.expunge(txn)
            return txns

    def upsert_plaid_transaction(
        self,
        external_id: str,
        source: str,
        account_id: str,
        posted_at: date,
        amount_cents: int,
        currency: str,
        merchant_descriptor: str | None,
        institution: str | None,
        original_descriptor: str | None = None,
        raw_name: str | None = None,
        counterparties: list[Any] | None = None,
        personal_finance_category: dict[str, Any] | None = None,
    ) -> PlaidTransaction:
        """Insert or update a Plaid transaction.

        Args:
            external_id: External transaction ID (e.g., Plaid transaction_id)
            source: Source identifier ("PLAID" or "CSV")
            account_id: Account ID
            posted_at: Posted date
            amount_cents: Amount in cents
            currency: Currency code
            merchant_descriptor: Merchant descriptor
            institution: Institution name
            original_descriptor: Plaid's raw ``original_description`` (rarely set)
            raw_name: Plaid's raw ``name`` (the fuller descriptor)
            counterparties: Plaid's structured counterparty list (verbatim)
            personal_finance_category: Plaid's own category guess (verbatim)

        Returns:
            Created or updated PlaidTransaction instance
        """
        with self.session() as session:  # type: Session
            plaid_txn = (
                session.query(PlaidTransaction)
                .filter(
                    PlaidTransaction.external_id == external_id,
                    PlaidTransaction.source == source,
                )
                .first()
            )

            if plaid_txn is None:
                plaid_txn = PlaidTransaction(
                    external_id=external_id,
                    source=source,
                    account_id=account_id,
                    posted_at=posted_at,
                    amount_cents=amount_cents,
                    currency=currency,
                    merchant_descriptor=merchant_descriptor,
                    original_descriptor=original_descriptor,
                    raw_name=raw_name,
                    counterparties=counterparties,
                    personal_finance_category=personal_finance_category,
                    institution=institution,
                )
                session.add(plaid_txn)
            else:
                plaid_txn.account_id = account_id
                plaid_txn.posted_at = posted_at
                plaid_txn.amount_cents = amount_cents
                plaid_txn.currency = currency
                plaid_txn.merchant_descriptor = merchant_descriptor
                plaid_txn.original_descriptor = original_descriptor
                plaid_txn.raw_name = raw_name
                plaid_txn.counterparties = counterparties
                plaid_txn.personal_finance_category = personal_finance_category
                plaid_txn.institution = institution
                plaid_txn.updated_at = datetime.now()

            session.flush()
            session.refresh(plaid_txn)
            session.expunge(plaid_txn)
            return plaid_txn

    def bulk_upsert_plaid_transactions(
        self,
        transactions: list[dict[str, Any]],
    ) -> list[int]:
        """Bulk insert or update Plaid transactions using PostgreSQL ON CONFLICT.

        Each dict should have keys:
            external_id, source, account_id, item_id, posted_at, amount_cents,
            currency, merchant_descriptor, institution

        Args:
            transactions: List of transaction dicts to upsert

        Returns:
            List of plaid_transaction_ids (in same order as input)
        """
        if not transactions:
            return []

        with self.session() as session:  # type: Session
            insert_stmt = pg_insert(PlaidTransaction).values(transactions)
            stmt = insert_stmt.on_conflict_do_update(
                index_elements=["external_id", "source"],
                set_={
                    "account_id": insert_stmt.excluded.account_id,
                    "item_id": insert_stmt.excluded.item_id,
                    "posted_at": insert_stmt.excluded.posted_at,
                    "amount_cents": insert_stmt.excluded.amount_cents,
                    "currency": insert_stmt.excluded.currency,
                    "merchant_descriptor": insert_stmt.excluded.merchant_descriptor,
                    "original_descriptor": insert_stmt.excluded.original_descriptor,
                    "raw_name": insert_stmt.excluded.raw_name,
                    "counterparties": insert_stmt.excluded.counterparties,
                    "personal_finance_category": (
                        insert_stmt.excluded.personal_finance_category
                    ),
                    "institution": insert_stmt.excluded.institution,
                    "updated_at": datetime.now(),
                },
            ).returning(PlaidTransaction.plaid_transaction_id)

            result = session.execute(stmt)
            plaid_ids = [row[0] for row in result.fetchall()]
            session.commit()
            return plaid_ids

    def get_plaid_transaction(
        self, plaid_transaction_id: int
    ) -> PlaidTransaction | None:
        """Get Plaid transaction by ID.

        Args:
            plaid_transaction_id: Plaid transaction ID

        Returns:
            PlaidTransaction instance or None if not found
        """
        with self.session() as session:  # type: Session
            plaid_txn = (
                session.query(PlaidTransaction)
                .filter(PlaidTransaction.plaid_transaction_id == plaid_transaction_id)
                .first()
            )
            if plaid_txn:
                session.expunge(plaid_txn)
            return plaid_txn

    def get_plaid_transactions_by_ids(
        self,
        plaid_transaction_ids: list[int],
    ) -> dict[int, PlaidTransaction]:
        """Get multiple Plaid transactions by IDs in a single query.

        Args:
            plaid_transaction_ids: List of Plaid transaction IDs

        Returns:
            Dict mapping plaid_transaction_id to PlaidTransaction instance
        """
        if not plaid_transaction_ids:
            return {}

        with self.session() as session:  # type: Session
            plaid_txns = (
                session.query(PlaidTransaction)
                .filter(
                    PlaidTransaction.plaid_transaction_id.in_(plaid_transaction_ids)
                )
                .all()
            )
            for txn in plaid_txns:
                session.expunge(txn)
            return {txn.plaid_transaction_id: txn for txn in plaid_txns}

    def delete_plaid_transactions_by_external_ids(
        self,
        external_ids: list[str],
        source: str = "PLAID",
    ) -> int:
        """Delete Plaid transactions by their external IDs.

        Cascade deletes to derived transactions automatically.

        Args:
            external_ids: List of external transaction IDs
            source: Source identifier (default: "PLAID")

        Returns:
            Number of transactions deleted
        """
        if not external_ids:
            return 0

        with self.session() as session:  # type: Session
            result = (
                session.query(PlaidTransaction)
                .filter(
                    PlaidTransaction.external_id.in_(external_ids),
                    PlaidTransaction.source == source,
                )
                .delete(synchronize_session=False)
            )
            return result

    def find_plaid_matches_for_investment_dedup(
        self,
        candidates: list[tuple[str, str, date, int]],
    ) -> set[tuple[str, str, date, int]]:
        """Find PLAID rows matching natural keys for investment dedup.

        Given a list of (item_id, account_id, posted_at, amount_cents) tuples,
        returns the subset that already have an existing ``source='PLAID'`` row.
        Used to skip inserting PLAID_INVESTMENT duplicates.

        Args:
            candidates: List of (item_id, account_id, posted_at, amount_cents) tuples.

        Returns:
            Set of tuples from *candidates* that have a matching PLAID row.
        """
        if not candidates:
            return set()

        with self.session() as session:  # type: Session
            # Build OR conditions for each candidate tuple
            from sqlalchemy import tuple_

            condition = tuple_(
                PlaidTransaction.item_id,
                PlaidTransaction.account_id,
                PlaidTransaction.posted_at,
                PlaidTransaction.amount_cents,
            ).in_(candidates)

            rows = (
                session.query(
                    PlaidTransaction.item_id,
                    PlaidTransaction.account_id,
                    PlaidTransaction.posted_at,
                    PlaidTransaction.amount_cents,
                )
                .filter(
                    PlaidTransaction.source == "PLAID",
                    condition,
                )
                .all()
            )

            return {(row[0], row[1], row[2], row[3]) for row in rows}

    def find_investment_dupes_with_plaid_match(self) -> list[PlaidTransaction]:
        """Find PLAID_INVESTMENT rows that duplicate existing PLAID rows.

        Self-joins ``plaid_transactions`` matching source='PLAID_INVESTMENT'
        against source='PLAID' on (item_id, account_id, posted_at, amount_cents).

        Returns:
            List of PLAID_INVESTMENT PlaidTransaction instances (expunged).
        """
        from sqlalchemy.orm import aliased

        inv = aliased(PlaidTransaction, name="inv")
        plaid = aliased(PlaidTransaction, name="plaid")

        with self.session() as session:  # type: Session
            dupes: list[PlaidTransaction] = (
                session.query(inv)
                .join(
                    plaid,
                    (inv.item_id == plaid.item_id)
                    & (inv.account_id == plaid.account_id)
                    & (inv.posted_at == plaid.posted_at)
                    & (inv.amount_cents == plaid.amount_cents),
                )
                .filter(
                    inv.source == "PLAID_INVESTMENT",
                    plaid.source == "PLAID",
                )
                .all()
            )
            for txn in dupes:
                session.expunge(txn)
            return dupes

    # Derived Transactions methods

    def insert_derived_transaction(self, data: dict[str, Any]) -> DerivedTransaction:
        """Insert a new derived transaction.

        Args:
            data: Derived transaction data dictionary with fields:
                - plaid_transaction_id, external_id, amount_cents, posted_at
                - merchant_descriptor (optional), merchant_id (optional)
                - category_id (optional)
                - web_search_summary (optional)
                - is_verified (default: False)

        Returns:
            Created DerivedTransaction instance
        """
        with self.session() as session:  # type: Session
            now = datetime.now()
            # Resolve merchant if merchant_descriptor is provided
            merchant_id = data.get("merchant_id")
            if (
                merchant_id is None
                and "merchant_descriptor" in data
                and data["merchant_descriptor"]
            ):
                normalized_name = normalize_merchant_name(data["merchant_descriptor"])
                merchant = (
                    session.query(Merchant)
                    .filter(Merchant.normalized_name == normalized_name)
                    .first()
                )
                if merchant is None:
                    merchant = Merchant(
                        normalized_name=normalized_name,
                        display_name=data["merchant_descriptor"],
                    )
                    session.add(merchant)
                    session.flush()
                merchant_id = merchant.merchant_id

            category_id = data.get("category_id")
            category_method = data.get("category_method")
            category_assigned_at = data.get("category_assigned_at")
            if category_id is not None and category_method is None:
                category_method = "manual"
            if category_id is not None and category_assigned_at is None:
                category_assigned_at = now

            derived_txn = DerivedTransaction(
                plaid_transaction_id=data["plaid_transaction_id"],
                external_id=data["external_id"],
                amount_cents=data["amount_cents"],
                posted_at=data["posted_at"],
                merchant_descriptor=data.get("merchant_descriptor"),
                merchant_id=merchant_id,
                category_id=category_id,
                category_model=data.get("category_model"),
                category_method=category_method,
                category_assigned_at=category_assigned_at,
                web_search_summary=data.get("web_search_summary"),
                is_verified=data.get("is_verified", False),
            )
            session.add(derived_txn)
            session.flush()
            if category_id is not None:
                key_by_id = self._resolve_category_keys(session, {category_id})
                category_key = key_by_id.get(category_id)
                if category_key is None:
                    raise ValueError(f"Category with ID {category_id} does not exist")
                method_for_event = cast(
                    CategoryMethod, category_method if category_method else "manual"
                )
                self._insert_category_event(
                    session,
                    transaction_id=derived_txn.transaction_id,
                    from_category_id=None,
                    to_category_id=category_id,
                    from_category_key=None,
                    to_category_key=category_key,
                    method=method_for_event,
                    model=data.get("category_model"),
                    reason=data.get("category_reason"),
                    created_at=category_assigned_at or now,
                )
            session.refresh(derived_txn)
            session.expunge(derived_txn)
            return derived_txn

    def bulk_insert_derived_transactions(
        self,
        payload_list: list[DerivedTransactionPayload],
    ) -> list[int]:
        """Bulk insert derived transactions efficiently.

        Optimized for performance:
        - Single query to resolve existing merchants
        - Bulk creation of new merchants
        - Bulk insert of all derived transactions

        Args:
            payload_list: Typed payloads produced by mutation plugins.

        Returns:
            List of created transaction IDs
        """
        if not payload_list:
            return []

        with self.session() as session:  # type: Session
            # Step 1: Collect unique merchant descriptors
            descriptors: set[str] = set()
            for payload in payload_list:
                if payload.merchant_id is None and payload.merchant_descriptor:
                    descriptors.add(
                        normalize_merchant_name(payload.merchant_descriptor)
                    )

            # Step 2: Fetch existing merchants in single query
            merchant_map: dict[str, int] = {}
            if descriptors:
                existing = (
                    session.query(Merchant)
                    .filter(Merchant.normalized_name.in_(descriptors))
                    .all()
                )
                for m in existing:
                    merchant_map[m.normalized_name] = m.merchant_id

            # Step 3: Create missing merchants
            new_merchants = []
            for payload in payload_list:
                if payload.merchant_id is None and payload.merchant_descriptor:
                    normalized = normalize_merchant_name(payload.merchant_descriptor)
                    if normalized not in merchant_map:
                        new_merchant = Merchant(
                            normalized_name=normalized,
                            display_name=payload.merchant_descriptor,
                        )
                        new_merchants.append(new_merchant)
                        merchant_map[normalized] = -1  # Placeholder

            if new_merchants:
                session.add_all(new_merchants)
                session.flush()
                # Update merchant_map with actual IDs
                for m in new_merchants:
                    merchant_map[m.normalized_name] = m.merchant_id

            # Step 4: Prepare derived transaction objects
            now = datetime.now()
            derived_txns = []
            for payload in payload_list:
                merchant_id = payload.merchant_id
                if merchant_id is None and payload.merchant_descriptor:
                    normalized = normalize_merchant_name(payload.merchant_descriptor)
                    merchant_id = merchant_map.get(normalized)

                category_id = payload.category_id
                category_method = payload.category_method
                category_assigned_at = payload.category_assigned_at
                if category_id is not None and category_method is None:
                    category_method = "manual"
                if category_id is not None and category_assigned_at is None:
                    category_assigned_at = now

                derived_txn = DerivedTransaction(
                    plaid_transaction_id=payload.plaid_transaction_id,
                    external_id=payload.external_id,
                    amount_cents=payload.amount_cents,
                    posted_at=payload.posted_at,
                    merchant_descriptor=payload.merchant_descriptor,
                    merchant_id=merchant_id,
                    category_id=category_id,
                    category_model=payload.category_model,
                    category_method=category_method,
                    category_assigned_at=category_assigned_at,
                    web_search_summary=payload.web_search_summary,
                    is_verified=payload.is_verified,
                    reporting_mode=payload.reporting_mode,
                    split_source=payload.split_source,
                    split_group_id=payload.split_group_id,
                    split_index=payload.split_index,
                )
                derived_txns.append(derived_txn)

            # Step 5: Bulk insert
            session.add_all(derived_txns)
            session.flush()

            # Step 6: Insert category events for rows with initial categories
            category_ids = {
                txn.category_id for txn in derived_txns if txn.category_id is not None
            }
            key_by_id = self._resolve_category_keys(session, set(category_ids))
            for txn, payload in zip(derived_txns, payload_list, strict=True):
                if txn.category_id is None:
                    continue
                category_key = key_by_id.get(txn.category_id)
                if category_key is None:
                    raise ValueError(
                        f"Category with ID {txn.category_id} does not exist"
                    )
                method_value = txn.category_method or "manual"
                self._insert_category_event(
                    session,
                    transaction_id=txn.transaction_id,
                    from_category_id=None,
                    to_category_id=txn.category_id,
                    from_category_key=None,
                    to_category_key=category_key,
                    method=cast(CategoryMethod, method_value),
                    model=txn.category_model,
                    reason=payload.category_reason,
                    created_at=txn.category_assigned_at or now,
                )

            items_by_transaction: list[tuple[int, list[TransactionItemPayload]]] = [
                (txn.transaction_id, payload.items)
                for txn, payload in zip(derived_txns, payload_list, strict=True)
                if payload.items
            ]
            if items_by_transaction:
                self._bulk_insert_transaction_items(session, items_by_transaction)

            # Get IDs
            transaction_ids = [txn.transaction_id for txn in derived_txns]

            return transaction_ids

    def _bulk_insert_transaction_items(
        self,
        session: Session,
        items_by_transaction: list[tuple[int, list[TransactionItemPayload]]],
    ) -> None:
        """Bulk insert transaction items in a single add_all call.

        Args:
            session: Active SQLAlchemy session (must be inside a session context).
            items_by_transaction: List of (transaction_id, items) tuples.
                All items across all transactions are inserted in one operation.
        """
        item_rows: list[TransactionItem] = []
        for transaction_id, item_payloads in items_by_transaction:
            for ip in item_payloads:
                item_rows.append(
                    TransactionItem(
                        transaction_id=transaction_id,
                        description=ip.description,
                        amount_cents=ip.amount_cents,
                        quantity=ip.quantity,
                        itemization_source=ip.itemization_source,
                        source_ref=ip.source_ref,
                    )
                )
        if item_rows:
            session.add_all(item_rows)
            session.flush()

    def bulk_insert_transaction_items(
        self,
        items_by_transaction: list[tuple[int, list[TransactionItemPayload]]],
    ) -> None:
        """Public facade for bulk-inserting transaction items.

        Inserts all items across all transactions in a single operation (no N+1).

        Args:
            items_by_transaction: List of (transaction_id, items) tuples.
        """
        if not items_by_transaction:
            return
        with self.session() as session:  # type: Session
            self._bulk_insert_transaction_items(session, items_by_transaction)

    def get_derived_by_plaid_id(
        self,
        plaid_transaction_id: int,
    ) -> list[DerivedTransaction]:
        """Get all derived transactions for a Plaid transaction.

        Args:
            plaid_transaction_id: Plaid transaction ID

        Returns:
            List of DerivedTransaction instances
        """
        with self.session() as session:  # type: Session
            derived_txns = (
                session.query(DerivedTransaction)
                .filter(DerivedTransaction.plaid_transaction_id == plaid_transaction_id)
                .all()
            )
            for txn in derived_txns:
                session.expunge(txn)
            return derived_txns

    def get_derived_by_plaid_ids(
        self,
        plaid_transaction_ids: list[int],
    ) -> dict[int, list[DerivedTransaction]]:
        """Get derived transactions for multiple Plaid transactions.

        Args:
            plaid_transaction_ids: List of Plaid transaction IDs

        Returns:
            Dict mapping plaid_transaction_id to list of DerivedTransaction instances
        """
        if not plaid_transaction_ids:
            return {}

        with self.session() as session:  # type: Session
            derived_txns = (
                session.query(DerivedTransaction)
                .filter(
                    DerivedTransaction.plaid_transaction_id.in_(plaid_transaction_ids)
                )
                .all()
            )
            for txn in derived_txns:
                session.expunge(txn)

            # Group by plaid_transaction_id
            result: dict[int, list[DerivedTransaction]] = {
                pid: [] for pid in plaid_transaction_ids
            }
            for txn in derived_txns:
                result[txn.plaid_transaction_id].append(txn)
            return result

    def get_uncategorized_derived_ids(
        self,
        *,
        source: str | None = None,
    ) -> list[int]:
        """Get IDs of derived transactions with no category.

        Args:
            source: Optional source filter (e.g., "XLSX_IMPORT", "PLAID_INVESTMENT").
                Joins to plaid_transactions to filter by source.

        Returns:
            List of transaction_ids where category_id IS NULL (excluding
            investment trades, which are recorded but never categorized).
        """
        with self.session() as session:  # type: Session
            query = session.query(DerivedTransaction.transaction_id).filter(
                DerivedTransaction.category_id.is_(None),
                _needs_categorization_clause(),
            )
            if source is not None:
                query = query.join(
                    PlaidTransaction,
                    DerivedTransaction.plaid_transaction_id
                    == PlaidTransaction.plaid_transaction_id,
                ).filter(PlaidTransaction.source == source)
            rows = query.order_by(DerivedTransaction.transaction_id).all()
            return [row[0] for row in rows]

    def derived_ids_created_since(self, since: datetime | None) -> list[int]:
        """Transaction ids for derived rows ingested after ``since`` (oldest first).

        Powers the eval cohort: the transactions that arrived since the previous
        eval run's high-water mark. Keyed on ``created_at`` (the ingest timestamp),
        NOT ``posted_at`` — Plaid back-dates pending->posted transitions and late
        arrivals, so a posted_at window would both miss and mis-bucket rows. When
        ``since`` is None, returns the full table (first run). Investment trades
        (``reporting_mode='DEFAULT_EXCLUDE'``) are excluded — they are never
        categorized, so they don't belong in the eval cohort either.

        ``household_id`` scopes the cohort to one household (all its users +
        visibilities). The eval passes it so the cohort matches its RLS-scoped
        snapshot of that same household — without it, the read-write role (which
        bypasses RLS) would return every household's rows and the eval's
        completeness guard would reject the run.
        """
        with self.session() as session:  # type: Session
            query = session.query(DerivedTransaction.transaction_id).filter(
                _needs_categorization_clause()
            )
            if since is not None:
                query = query.filter(DerivedTransaction.created_at > since)
            rows = query.order_by(
                DerivedTransaction.created_at.asc(),
                DerivedTransaction.transaction_id.asc(),
            ).all()
            return [row[0] for row in rows]

    def max_created_at_for_ids(self, transaction_ids: list[int]) -> datetime | None:
        """Max ``created_at`` over the given derived rows (the next run's watermark)."""
        if not transaction_ids:
            return None
        with self.session() as session:  # type: Session
            return (
                session.query(func.max(DerivedTransaction.created_at))
                .filter(DerivedTransaction.transaction_id.in_(transaction_ids))
                .scalar()
            )

    # ------------------------------------------------------------------ eval store

    def last_eval_watermark(self) -> datetime | None:
        """High-water mark (max ``cohort_max_created_at``) over completed eval runs.

        The next eval cohort is everything ingested strictly after this.
        ``household_id`` scopes the watermark to one household's runs so a
        completed run for another household never gates this cohort — it MUST be
        passed alongside a household-scoped cohort. When None (dev/SQLite, no
        RLS), the watermark is global.
        """
        with self.session() as session:  # type: Session
            query = session.query(func.max(EvalRun.cohort_max_created_at)).filter(
                EvalRun.status == "completed"
            )
            return query.scalar()

    @staticmethod
    def _eval_item_rows(
        eval_run_id: int, items: list[dict[str, Any]]
    ) -> list[EvalItem]:
        return [
            EvalItem(
                eval_run_id=eval_run_id,
                transaction_id=item["transaction_id"],
                merchant_descriptor=item.get("merchant_descriptor"),
                legacy_key=item.get("legacy_key"),
                agent_key=item.get("agent_key"),
                agent_reasoning=item.get("agent_reasoning"),
                agent_confidence=item.get("agent_confidence"),
                method_at_eval_time=item["method_at_eval_time"],
                trace_link=item.get("trace_link"),
            )
            for item in items
        ]

    def record_eval_run(
        self,
        *,
        run_at: datetime,
        status: str,
        cohort_size: int,
        cohort_max_created_at: datetime | None = None,
        branch_name: str | None = None,
        r2_fixture_url: str | None = None,
        version: dict[str, Any] | None = None,
        items: list[dict[str, Any]] | None = None,
    ) -> int:
        """Insert an ``eval_runs`` row (plus its ``items``, atomically) and return its id.

        Passing ``items`` writes the run and its eval_items in ONE transaction, so
        a ``completed`` row (which advances the watermark via
        ``cohort_max_created_at``) never lands without its items — a partial write
        can't skip the cohort while claiming success. ``household_id`` scopes the
        run so ``last_eval_watermark`` resolves per household.
        """
        version = version or {}
        with self.session() as session:  # type: Session
            run = EvalRun(
                run_at=run_at,
                status=status,
                cohort_size=cohort_size,
                cohort_max_created_at=cohort_max_created_at,
                branch_name=branch_name,
                r2_fixture_url=r2_fixture_url,
                model=version.get("model"),
                prompt_version=version.get("prompt_version"),
                harness_sha=version.get("harness_sha"),
                taxonomy_version=version.get("taxonomy_version"),
                rules_version=version.get("rules_version"),
            )
            session.add(run)
            session.flush()
            if items:
                session.add_all(self._eval_item_rows(run.eval_run_id, items))
            return run.eval_run_id

    def record_eval_items(self, eval_run_id: int, items: list[dict[str, Any]]) -> None:
        """Bulk-insert ``eval_items`` rows for a run."""
        if not items:
            return
        with self.session() as session:  # type: Session
            session.add_all(self._eval_item_rows(eval_run_id, items))

    def set_eval_run_fixture(self, eval_run_id: int, r2_fixture_url: str) -> None:
        """Record the R2 fixture URL once the branch has been dumped and uploaded."""
        with self.session() as session:  # type: Session
            run = session.get(EvalRun, eval_run_id)
            if run is not None:
                run.r2_fixture_url = r2_fixture_url

    def eval_items_with_verdicts(
        self, *, settled_before: datetime | None = None
    ) -> list[dict[str, Any]]:
        """All eval items enriched with a verdict derived from your corrections.

        There is no stored verdict (no staging step). For each item:
        - ``corrected`` if the transaction has a ``manual`` category event created
          after the run's ``run_at`` (you recategorized it post-eval); ``human_key``
          is that latest post-run event's ``to_category_key``.
        - ``confirmed`` if no such correction exists AND the run is settled
          (``run_at <= settled_before``); ``human_key`` is the agent's key.
        - ``provisional`` otherwise (too new to have been reviewed).

        ``settled_before`` is "now minus the settling window"; pass None to treat
        every uncorrected item as still provisional.
        """
        from collections import defaultdict

        with self.session() as session:  # type: Session
            runs = {r.eval_run_id: r for r in session.query(EvalRun).all()}
            items = session.query(EvalItem).all()
            manual = (
                session.query(
                    TransactionCategoryEvent.transaction_id,
                    TransactionCategoryEvent.created_at,
                    TransactionCategoryEvent.to_category_key,
                )
                .filter(TransactionCategoryEvent.method == "manual")
                .order_by(TransactionCategoryEvent.created_at.desc())
                .all()
            )
            by_txn: dict[int, list[tuple[datetime, str]]] = defaultdict(list)
            for tid, created, to_key in manual:
                if created is not None:
                    by_txn[tid].append((created, to_key))

            out: list[dict[str, Any]] = []
            for item in items:
                run = runs.get(item.eval_run_id)
                if run is None:
                    continue
                post = [
                    c for c in by_txn.get(item.transaction_id, []) if c[0] > run.run_at
                ]
                if post:
                    verdict = "corrected"
                    human_key = post[0][1]  # newest (events are desc-ordered)
                elif settled_before is not None and run.run_at <= settled_before:
                    verdict = "confirmed"
                    human_key = item.agent_key
                else:
                    verdict = "provisional"
                    human_key = None
                out.append(
                    {
                        "eval_run_id": item.eval_run_id,
                        "run_at": run.run_at,
                        "transaction_id": item.transaction_id,
                        "legacy_key": item.legacy_key,
                        "agent_key": item.agent_key,
                        "method_at_eval_time": item.method_at_eval_time,
                        "agent_confidence": item.agent_confidence,
                        "verdict": verdict,
                        "human_key": human_key,
                        "model": run.model,
                        "prompt_version": run.prompt_version,
                        "harness_sha": run.harness_sha,
                        "taxonomy_version": run.taxonomy_version,
                        "rules_version": run.rules_version,
                    }
                )
            return out

    def get_derived_transactions_by_ids(
        self,
        transaction_ids: list[int],
    ) -> list[DerivedTransaction]:
        """Get derived transactions by IDs.

        Args:
            transaction_ids: List of transaction IDs

        Returns:
            List of DerivedTransaction instances with ``plaid_transaction``
            eager-loaded but restricted to ``raw_name`` (via ``load_only``), so
            the categorizer sweep can read ``txn.plaid_transaction.raw_name``
            after the session closes without a second round trip and without
            hydrating the plaid row's JSON blobs. Other ``plaid_transaction``
            columns and every other relationship remain deferred/lazy and will
            raise ``DetachedInstanceError`` if accessed.
        """
        if not transaction_ids:
            return []

        with self.session() as session:  # type: Session
            derived_txns = (
                session.query(DerivedTransaction)
                .options(
                    joinedload(DerivedTransaction.plaid_transaction).load_only(
                        PlaidTransaction.raw_name
                    )
                )
                .filter(DerivedTransaction.transaction_id.in_(transaction_ids))
                .order_by(DerivedTransaction.external_id)  # Deterministic for cache
                .all()
            )
            for txn in derived_txns:
                # Expunge the joined parent before the txn so neither is expired
                # by the implicit commit on session exit.
                if txn.plaid_transaction is not None:
                    session.expunge(txn.plaid_transaction)
                session.expunge(txn)
            return derived_txns

    def delete_derived_by_plaid_ids(
        self,
        plaid_transaction_ids: list[int],
    ) -> int:
        """Delete all derived transactions for multiple Plaid transactions.

        Args:
            plaid_transaction_ids: List of Plaid transaction IDs

        Returns:
            Number of transactions deleted
        """
        if not plaid_transaction_ids:
            return 0
        with self.session() as session:  # type: Session
            result = (
                session.query(DerivedTransaction)
                .filter(
                    DerivedTransaction.plaid_transaction_id.in_(plaid_transaction_ids)
                )
                .delete(synchronize_session=False)
            )
            return result

    def bulk_update_derived_categories(
        self,
        updates: dict[int, int],
        *,
        method: CategoryMethod = "llm",
        model: str | None = None,
        reason: str | None = None,
    ) -> int:
        """Bulk update categories for multiple derived transactions.

        Performs all updates and event inserts in a single DB transaction.

        Args:
            updates: Dictionary mapping transaction_id to category_id

        Returns:
            Number of transactions updated
        """
        if not updates:
            return 0

        with self.session() as session:  # type: Session
            return self._apply_category_updates(
                session,
                updates=updates,
                method=method,
                reason=reason,
                model=model,
            )

    def bulk_update_derived_reporting_mode(
        self,
        updates: dict[int, str],
    ) -> int:
        """Bulk update reporting_mode for multiple derived transactions.

        Performs all updates in a single database transaction for efficiency.

        Args:
            updates: Dictionary mapping transaction_id to reporting_mode
                (e.g., "DEFAULT_INCLUDE" or "DEFAULT_EXCLUDE")

        Returns:
            Number of transactions updated
        """
        if not updates:
            return 0

        with self.session() as session:  # type: Session
            now = datetime.now()
            # Build CASE expression for reporting_mode
            case_expr = case(
                updates,
                value=DerivedTransaction.transaction_id,
            )
            # Update all matching transactions in single query
            result: int = (
                session.query(DerivedTransaction)
                .filter(DerivedTransaction.transaction_id.in_(updates.keys()))
                .update(
                    {
                        DerivedTransaction.reporting_mode: case_expr,
                        DerivedTransaction.updated_at: now,
                    },
                    synchronize_session=False,
                )
            )
            return result

    def bulk_update_derived_web_search_summaries(
        self,
        updates: dict[int, str | None],
    ) -> int:
        """Bulk update LLM merchant research summaries for derived transactions.

        Args:
            updates: Dictionary mapping transaction_id to summary text (or None)

        Returns:
            Number of transactions updated
        """
        if not updates:
            return 0

        with self.session() as session:  # type: Session
            now = datetime.now()
            case_expr = case(
                updates,
                value=DerivedTransaction.transaction_id,
            )
            result: int = (
                session.query(DerivedTransaction)
                .filter(DerivedTransaction.transaction_id.in_(updates.keys()))
                .update(
                    {
                        DerivedTransaction.web_search_summary: case_expr,
                        DerivedTransaction.updated_at: now,
                    },
                    synchronize_session=False,
                )
            )
            return result

    def update_derived_mutable(
        self,
        transaction_id: int,
        updates: dict[str, Any],
    ) -> DerivedTransaction:
        """Update mutable fields of a derived transaction.

        Only updates if transaction is not verified (is_verified=False).

        Args:
            transaction_id: Transaction ID to update
            updates: Dictionary of fields to update

        Returns:
            Updated DerivedTransaction instance

        Raises:
            ValueError: If transaction is verified and cannot be updated
        """
        with self.session() as session:  # type: Session
            derived_txn = (
                session.query(DerivedTransaction)
                .filter(DerivedTransaction.transaction_id == transaction_id)
                .first()
            )
            if derived_txn is None:
                raise ValueError(f"Derived transaction {transaction_id} not found")

            if derived_txn.is_verified:
                raise ValueError(
                    f"Transaction {transaction_id} is verified and cannot be updated"
                )

            # Resolve merchant if merchant_descriptor is provided
            if "merchant_descriptor" in updates and updates["merchant_descriptor"]:
                normalized_name = normalize_merchant_name(
                    updates["merchant_descriptor"]
                )
                merchant = (
                    session.query(Merchant)
                    .filter(Merchant.normalized_name == normalized_name)
                    .first()
                )
                if merchant is None:
                    merchant = Merchant(
                        normalized_name=normalized_name,
                        display_name=updates["merchant_descriptor"],
                    )
                    session.add(merchant)
                    session.flush()
                updates["merchant_id"] = merchant.merchant_id

            previous_category_id = derived_txn.category_id
            category_update_id = updates.get("category_id")
            category_changed = (
                category_update_id is not None
                and category_update_id != previous_category_id
            )
            if category_update_id is not None and "category_method" not in updates:
                updates["category_method"] = "manual"
            if category_update_id is not None and "category_assigned_at" not in updates:
                updates["category_assigned_at"] = datetime.now()

            # Update mutable fields
            for key, value in updates.items():
                if key in (
                    "category_id",
                    "category_model",
                    "category_method",
                    "category_assigned_at",
                    "merchant_id",
                    "amount_cents",
                    "merchant_descriptor",
                    "web_search_summary",
                    "is_verified",
                ):
                    setattr(derived_txn, key, value)

            derived_txn.updated_at = datetime.now()
            session.flush()
            if category_changed:
                from_category_id = previous_category_id
                to_category_id = cast(int, category_update_id)
                key_by_id = self._resolve_category_keys(
                    session,
                    {to_category_id}
                    if from_category_id is None
                    else {from_category_id, to_category_id},
                )
                to_category_key = key_by_id.get(to_category_id)
                if to_category_key is None:
                    raise ValueError(
                        f"Category with ID {to_category_id} does not exist"
                    )
                from_category_key = (
                    key_by_id.get(from_category_id)
                    if from_category_id is not None
                    else None
                )
                method_value = updates.get("category_method", "manual")
                model_value = cast(str | None, updates.get("category_model"))
                assigned_at = cast(
                    datetime, updates.get("category_assigned_at", datetime.now())
                )
                self._insert_category_event(
                    session,
                    transaction_id=derived_txn.transaction_id,
                    from_category_id=from_category_id,
                    to_category_id=to_category_id,
                    from_category_key=from_category_key,
                    to_category_key=to_category_key,
                    method=cast(CategoryMethod, method_value),
                    model=model_value,
                    reason=cast(str | None, updates.get("category_reason")),
                    created_at=assigned_at,
                )
            session.refresh(derived_txn)
            session.expunge(derived_txn)
            return derived_txn

    def delete_plaid_item(self, item_id: str) -> bool:
        """Delete a Plaid item.

        Args:
            item_id: Plaid item ID to delete

        Returns:
            True if item was deleted, False if not found
        """
        with self.session() as session:  # type: Session
            item = session.query(PlaidItem).filter_by(item_id=item_id).first()
            if item is None:
                return False
            session.delete(item)
            return True

    # Migration methods

    def get_transactions_by_category_id(
        self, category_id: int
    ) -> list[DerivedTransaction]:
        """
        Get all transactions for a given category.

        Args:
            category_id: Category ID to filter by

        Returns:
            List of DerivedTransaction instances with `plaid_transaction`
            eager-loaded so callers can read its attributes after the session
            closes. Other relationships remain lazy and will raise
            DetachedInstanceError if accessed.
        """
        with self.session() as session:  # type: Session
            txns = (
                session.query(DerivedTransaction)
                .options(joinedload(DerivedTransaction.plaid_transaction))
                .filter_by(category_id=category_id)
                .all()
            )
            for txn in txns:
                # Expunge the joined parent before the txn so neither is
                # expired by the implicit commit on session exit.
                session.expunge(txn.plaid_transaction)
                session.expunge(txn)
            return txns

    def update_category_key(self, old_key: str, new_key: str) -> None:
        """
        Update category key (for rename operations).

        Only active categories are considered: an archived ghost row with the
        same `new_key` does not block the rename, and a deprecated row at
        `old_key` is treated as not-found.

        Args:
            old_key: Current category key
            new_key: New category key

        Raises:
            ValueError: If old_key does not exist or new_key already exists
        """
        with self.session() as session:  # type: Session
            old_category = (
                session.query(Category)
                .filter(Category.key == old_key, Category.deprecated_at.is_(None))
                .first()
            )
            if not old_category:
                msg = f"Category with key '{old_key}' does not exist"
                raise ValueError(msg)

            new_category = (
                session.query(Category)
                .filter(Category.key == new_key, Category.deprecated_at.is_(None))
                .first()
            )
            if new_category:
                msg = f"Category with key '{new_key}' already exists"
                raise ValueError(msg)

            old_category.key = new_key

    def reassign_transactions_to_category(
        self,
        transaction_ids: list[int],
        new_category_id: int,
        reset_verified: bool = False,
        *,
        reason: str | None = "taxonomy_migration",
    ) -> None:
        """
        Bulk reassign transactions to new category, optionally reset verified status.

        Args:
            transaction_ids: List of transaction IDs to reassign
            new_category_id: New category ID to assign
            reset_verified: If True, set is_verified=False for these transactions

        Raises:
            ValueError: If new_category_id does not exist
        """
        if not transaction_ids:
            return

        with self.session() as session:  # type: Session
            # Validate category exists and is active. Deprecated categories
            # cannot accept new transaction assignments.
            category = (
                session.query(Category)
                .filter(
                    Category.category_id == new_category_id,
                    Category.deprecated_at.is_(None),
                )
                .first()
            )
            if not category:
                msg = f"Category with ID {new_category_id} does not exist"
                raise ValueError(msg)

            updates = dict.fromkeys(transaction_ids, new_category_id)
            self._apply_category_updates(
                session,
                updates=updates,
                method="taxonomy_migration",
                reason=reason,
                preserve_model=True,
                is_verified=False if reset_verified else None,
            )

    def replace_categories_from_taxonomy(self, taxonomy: Any) -> None:
        """
        Sync categories in DB with taxonomy contents, preserving category IDs.

        Categories that drop out of the taxonomy are soft-deleted via
        `deprecated_at`, not hard-deleted, so audit-trail rows in
        `transaction_category_events` keep referential integrity. If the
        user re-adds a previously deprecated key, the existing row is
        resurrected (`deprecated_at` reset to NULL) so its `category_id` —
        and any historical events pointing at it — stay linked to the same
        logical category.

        Args:
            taxonomy: Taxonomy instance to sync from
        """
        now = datetime.now()
        with self.session() as session:  # type: Session
            # Load all rows (active + deprecated) so we can resurrect on key reuse.
            existing_by_key = {c.key: c for c in session.query(Category)}
            existing_active = {
                key: cat
                for key, cat in existing_by_key.items()
                if cat.deprecated_at is None
            }

            all_nodes = taxonomy.all_nodes()
            new_keys = {node.key for node in all_nodes}

            # Deprecate active rows that fell out of the taxonomy.
            keys_to_deprecate = set(existing_active) - new_keys
            for key in keys_to_deprecate:
                existing_active[key].deprecated_at = now
            session.flush()

            # Build parent_id mapping seeded with currently-active rows we keep.
            parent_id_map: dict[str, int] = {
                key: cat.category_id
                for key, cat in existing_active.items()
                if key in new_keys
            }

            parent_nodes = [node for node in all_nodes if node.parent_key is None]
            child_nodes = [node for node in all_nodes if node.parent_key is not None]

            for node in parent_nodes:
                cat = self._upsert_or_resurrect_category(
                    session,
                    node=node,
                    parent_id=None,
                    existing_by_key=existing_by_key,
                )
                parent_id_map[node.key] = cat.category_id

            for node in child_nodes:
                parent_id = (
                    parent_id_map.get(node.parent_key) if node.parent_key else None
                )
                cat = self._upsert_or_resurrect_category(
                    session,
                    node=node,
                    parent_id=parent_id,
                    existing_by_key=existing_by_key,
                )
                parent_id_map[node.key] = cat.category_id

    def _upsert_or_resurrect_category(
        self,
        session: Session,
        *,
        node: Any,
        parent_id: int | None,
        existing_by_key: dict[str, Category],
    ) -> Category:
        """Update active row, resurrect deprecated row, or insert a new one."""
        cat = existing_by_key.get(node.key)
        if cat is not None:
            # Resurrect deprecated rows so the existing category_id (and any
            # transaction_category_events that reference it) stays linked.
            if cat.deprecated_at is not None:
                cat.deprecated_at = None
            cat.name = node.name
            cat.description = node.description
            cat.parent_id = parent_id
            return cat

        cat = Category(
            key=node.key,
            name=node.name,
            description=node.description,
            parent_id=parent_id,
        )
        session.add(cat)
        session.flush()
        existing_by_key[node.key] = cat
        return cat

    # Amazon Order methods

    def upsert_amazon_order(
        self,
        order_id: str,
        order_date: date,
        order_total_cents: int,
        *,
        profile_id: int,
        tax_cents: int = 0,
        shipping_cents: int = 0,
    ) -> AmazonOrderDB:
        """Upsert an Amazon order.

        Args:
            order_id: Amazon order ID (e.g., "113-5524816-2451403")
            order_date: Date of the order
            order_total_cents: Total amount in cents
            profile_id: ID of the amazon_login_profiles row that produced this
                scrape. Last-writer-wins on re-scrape under a different profile.
            tax_cents: Tax amount in cents
            shipping_cents: Shipping amount in cents

        Returns:
            Created or updated AmazonOrderDB instance
        """
        with self.session() as session:  # type: Session
            order = (
                session.query(AmazonOrderDB)
                .filter(AmazonOrderDB.order_id == order_id)
                .first()
            )

            if order is None:
                order = AmazonOrderDB(
                    order_id=order_id,
                    profile_id=profile_id,
                    order_date=order_date,
                    order_total_cents=order_total_cents,
                    tax_cents=tax_cents,
                    shipping_cents=shipping_cents,
                )
                session.add(order)
            else:
                order.profile_id = profile_id
                order.order_date = order_date
                order.order_total_cents = order_total_cents
                order.tax_cents = tax_cents
                order.shipping_cents = shipping_cents
                order.updated_at = datetime.now()

            session.flush()
            session.refresh(order)
            session.expunge(order)
            return order

    def upsert_amazon_item(
        self,
        order_id: str,
        asin: str,
        description: str,
        price_cents: int,
        quantity: int = 1,
    ) -> AmazonItemDB:
        """Upsert an Amazon item.

        Args:
            order_id: Amazon order ID this item belongs to
            asin: Amazon Standard Identification Number
            description: Item description
            price_cents: Price per item in cents
            quantity: Number of items

        Returns:
            Created or updated AmazonItemDB instance
        """
        with self.session() as session:  # type: Session
            item = (
                session.query(AmazonItemDB)
                .filter(
                    AmazonItemDB.order_id == order_id,
                    AmazonItemDB.asin == asin,
                )
                .first()
            )

            if item is None:
                item = AmazonItemDB(
                    order_id=order_id,
                    asin=asin,
                    description=description,
                    price_cents=price_cents,
                    quantity=quantity,
                )
                session.add(item)
            else:
                item.description = description
                item.price_cents = price_cents
                item.quantity = quantity
                item.updated_at = datetime.now()

            session.flush()
            session.refresh(item)
            session.expunge(item)
            return item

    def get_amazon_order(self, order_id: str) -> AmazonOrderDB | None:
        """Get an Amazon order by ID.

        Args:
            order_id: Amazon order ID

        Returns:
            AmazonOrderDB instance or None if not found
        """
        with self.session() as session:  # type: Session
            order = (
                session.query(AmazonOrderDB)
                .filter(AmazonOrderDB.order_id == order_id)
                .first()
            )
            if order:
                session.expunge(order)
            return order

    def list_amazon_orders(
        self, *, profile_id: int | None = None
    ) -> list[AmazonOrderDB]:
        """List Amazon orders, optionally filtered by profile.

        Args:
            profile_id: When provided, only orders attributed to this profile
                are returned.

        Returns:
            List of AmazonOrderDB instances
        """
        with self.session() as session:  # type: Session
            query = session.query(AmazonOrderDB)
            if profile_id is not None:
                query = query.filter(AmazonOrderDB.profile_id == profile_id)
            orders = query.all()
            for order in orders:
                session.expunge(order)
            return orders

    def get_amazon_items_for_order(self, order_id: str) -> list[AmazonItemDB]:
        """Get all items for an Amazon order.

        Args:
            order_id: Amazon order ID

        Returns:
            List of AmazonItemDB instances
        """
        with self.session() as session:  # type: Session
            items = (
                session.query(AmazonItemDB)
                .filter(AmazonItemDB.order_id == order_id)
                .all()
            )
            for item in items:
                session.expunge(item)
            return items

    def list_amazon_login_profiles(
        self, *, enabled_only: bool = False
    ) -> list[AmazonLoginProfileDB]:
        """List all Amazon login profiles ordered by sort_order, profile_id."""
        with self.session() as session:  # type: Session
            query = session.query(AmazonLoginProfileDB)
            if enabled_only:
                query = query.filter(AmazonLoginProfileDB.enabled.is_(True))
            profiles = query.order_by(
                AmazonLoginProfileDB.sort_order.asc(),
                AmazonLoginProfileDB.profile_id.asc(),
            ).all()
            session.expunge_all()
            return profiles

    def create_amazon_login_profile(
        self,
        *,
        profile_key: str,
        display_name: str,
        enabled: bool = True,
        sort_order: int = 0,
    ) -> AmazonLoginProfileDB:
        """Create a new Amazon login profile."""
        with self.session() as session:  # type: Session
            profile = AmazonLoginProfileDB(
                profile_key=profile_key,
                display_name=display_name,
                enabled=enabled,
                sort_order=sort_order,
            )
            session.add(profile)
            session.commit()
            session.refresh(profile)
            session.expunge(profile)
            return profile

    def update_amazon_login_profile(
        self,
        *,
        profile_key: str,
        display_name: str | None = None,
        enabled: bool | None = None,
        sort_order: int | None = None,
    ) -> AmazonLoginProfileDB:
        """Update fields on an existing Amazon login profile."""
        with self.session() as session:  # type: Session
            profile = (
                session.query(AmazonLoginProfileDB)
                .filter(AmazonLoginProfileDB.profile_key == profile_key)
                .one_or_none()
            )
            if profile is None:
                raise ValueError(f"Amazon login profile not found: {profile_key!r}")
            if display_name is not None:
                profile.display_name = display_name
            if enabled is not None:
                profile.enabled = enabled
            if sort_order is not None:
                profile.sort_order = sort_order
            profile.updated_at = datetime.utcnow()
            session.commit()
            session.refresh(profile)
            session.expunge(profile)
            return profile

    def delete_amazon_login_profile(self, *, profile_key: str) -> None:
        """Delete an Amazon login profile by key.

        Raises:
            ValueError: If the profile does not exist, or if it has attributed
                amazon_orders rows blocking the FK delete (RESTRICT).
        """
        with self.session() as session:  # type: Session
            profile = (
                session.query(AmazonLoginProfileDB)
                .filter(AmazonLoginProfileDB.profile_key == profile_key)
                .one_or_none()
            )
            if profile is None:
                raise ValueError(f"Amazon login profile not found: {profile_key!r}")
            attributed_count = (
                session.query(AmazonOrderDB)
                .filter(AmazonOrderDB.profile_id == profile.profile_id)
                .count()
            )
            session.delete(profile)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise ValueError(
                    f"cannot delete profile {profile_key!r}: it has "
                    f"{attributed_count} attributed orders; reassign or wipe "
                    "them first"
                ) from exc

    def set_amazon_login_context_id(
        self, *, profile_key: str, context_id: str | None
    ) -> AmazonLoginProfileDB:
        """Set or clear the Browserbase context ID for a profile."""
        with self.session() as session:  # type: Session
            profile = (
                session.query(AmazonLoginProfileDB)
                .filter(AmazonLoginProfileDB.profile_key == profile_key)
                .one_or_none()
            )
            if profile is None:
                raise ValueError(f"Amazon login profile not found: {profile_key!r}")
            profile.browserbase_context_id = context_id
            profile.updated_at = datetime.utcnow()
            session.commit()
            session.refresh(profile)
            session.expunge(profile)
            return profile

    def record_amazon_login_auth_result(
        self,
        *,
        profile_key: str,
        status: str,
        error: str | None = None,
    ) -> None:
        """Record the result of a login auth attempt for a profile."""
        with self.session() as session:  # type: Session
            profile = (
                session.query(AmazonLoginProfileDB)
                .filter(AmazonLoginProfileDB.profile_key == profile_key)
                .one_or_none()
            )
            if profile is None:
                raise ValueError(f"Amazon login profile not found: {profile_key!r}")
            profile.last_auth_at = datetime.utcnow()
            profile.last_auth_status = status
            profile.last_auth_error = error
            profile.updated_at = datetime.utcnow()
            session.commit()

    def set_amazon_profile_history_watermark(
        self,
        *,
        profile_id: int,
        through_date: date,
    ) -> None:
        """Mark a profile's history as fully scraped through ``through_date``."""
        with self.session() as session:  # type: Session
            profile = (
                session.query(AmazonLoginProfileDB)
                .filter(AmazonLoginProfileDB.profile_id == profile_id)
                .one_or_none()
            )
            if profile is None:
                raise ValueError(
                    f"Amazon login profile not found: profile_id={profile_id}"
                )
            profile.history_complete_through = through_date
            profile.updated_at = datetime.utcnow()
            session.commit()

    def get_amazon_login_profile(
        self, *, profile_key: str
    ) -> AmazonLoginProfileDB | None:
        """Get a single Amazon login profile by key."""
        with self.session() as session:  # type: Session
            profile = (
                session.query(AmazonLoginProfileDB)
                .filter(AmazonLoginProfileDB.profile_key == profile_key)
                .one_or_none()
            )
            if profile is not None:
                session.expunge(profile)
            return profile

    def get_amazon_order_for_plaid_txn(
        self, session: Session, plaid_transaction_id: int
    ) -> AmazonOrderDB | None:
        """Return the matched Amazon order for a Plaid transaction, or None.

        Loads the PlaidTransaction from the given session (no second session
        opened), then uses the existing AmazonOrderIndex / match_orders_to_transactions
        logic to find the first matching order. Returns the AmazonOrderDB row, or
        None if no match exists.

        Args:
            session: Caller's active SQLAlchemy session.
            plaid_transaction_id: PK of the plaid_transaction to check.

        Returns:
            The matched AmazonOrderDB instance if one exists, else None.
        """
        from penny.adapters.amazon.order_index import AmazonOrderIndex
        from penny.adapters.amazon.plaid_matcher import match_orders_to_transactions

        plaid_txn = session.get(PlaidTransaction, plaid_transaction_id)
        if plaid_txn is None:
            return None

        index = AmazonOrderIndex.from_db(self)
        if index.order_count == 0:
            return None

        orders = index.list_orders()
        matches = match_orders_to_transactions(orders, [plaid_txn])

        for order_id, matched_plaid_id in matches.items():
            if matched_plaid_id == plaid_transaction_id:
                order = (
                    session.query(AmazonOrderDB)
                    .filter(AmazonOrderDB.order_id == order_id)
                    .first()
                )
                return order

        return None

    def create_refund_link(
        self,
        session: Session,
        refund_txn_id: int,
        original_txn_id: int,
        matched_by: str,
        matched_at: datetime,
    ) -> None:
        """Write all three refund link fields atomically on an already-loaded row.

        The row must already be loaded in the caller's session; this method
        performs only the three-column update, no validation. Caller owns the
        session and is responsible for committing.

        Args:
            session: Caller's active SQLAlchemy session.
            refund_txn_id: PK of the derived_transaction that is the refund.
            original_txn_id: PK of the original derived_transaction being refunded.
            matched_by: Who created the link ('user' or 'auto').
            matched_at: UTC timestamp when the link was established.
        """
        row = session.get(DerivedTransaction, refund_txn_id)
        if row is None:
            return
        row.refund_of_transaction_id = original_txn_id
        row.refund_matched_by = matched_by
        row.refund_matched_at = matched_at

    _DEFAULT_SIGN_CONVENTION: str = "expense_positive"

    def get_sign_convention(self, account_id: str) -> str:
        """Return the sign convention for an account.

        Returns 'expense_positive' as the default when no row exists.
        Logs a WARNING when the default is used — every known account should
        have been seeded by the time this is called.

        Args:
            account_id: The Plaid account_id to look up.

        Returns:
            'expense_positive' or 'expense_negative'.
        """
        with self.session() as session:  # type: Session
            row = session.get(AccountSignConvention, account_id)
            if row is None:
                logger.bind(account_id=account_id).warning(
                    "No sign convention found for account {}; falling back to '{}'",
                    account_id,
                    self._DEFAULT_SIGN_CONVENTION,
                )
                return self._DEFAULT_SIGN_CONVENTION
            convention: str = row.sign_convention
            return convention

    def bulk_get_sign_conventions(self, account_ids: list[str]) -> dict[str, str]:
        """Return {account_id: convention} for all given account_ids.

        Missing account_ids receive the default 'expense_positive'.

        Args:
            account_ids: List of Plaid account_ids to look up.

        Returns:
            Dict mapping each account_id to its convention string.
        """
        if not account_ids:
            return {}
        with self.session() as session:  # type: Session
            rows = (
                session.query(AccountSignConvention)
                .filter(AccountSignConvention.account_id.in_(account_ids))
                .all()
            )
            result: dict[str, str] = {
                row.account_id: row.sign_convention for row in rows
            }
        for account_id in account_ids:
            if account_id not in result:
                logger.bind(account_id=account_id).warning(
                    "No sign convention found for account {}; falling back to '{}'",
                    account_id,
                    self._DEFAULT_SIGN_CONVENTION,
                )
                result[account_id] = self._DEFAULT_SIGN_CONVENTION
        return result

    def set_sign_convention(
        self,
        account_id: str,
        sign_convention: str,
        *,
        provenance: str = "manual",
        notes: str | None = None,
    ) -> None:
        """Upsert a sign convention for an account.

        ON CONFLICT updates sign_convention, provenance, updated_at, and notes;
        account_id and created_at are preserved.
        When notes is None (omitted), the existing notes value is left unchanged
        on the update path.

        Args:
            account_id: The Plaid account_id to set a convention for.
            sign_convention: 'expense_positive' or 'expense_negative'.
            provenance: 'manual' or 'seeded'. Defaults to 'manual'.
            notes: Optional free-text note about the convention.
                   When omitted (None), existing notes are preserved on update.
        """
        with self.session() as session:  # type: Session
            row = session.get(AccountSignConvention, account_id)
            if row is None:
                row = AccountSignConvention(
                    account_id=account_id,
                    sign_convention=sign_convention,
                    provenance=provenance,
                    notes=notes,
                )
                session.add(row)
            else:
                row.sign_convention = sign_convention
                row.provenance = provenance
                row.updated_at = datetime.utcnow()
                if notes is not None:
                    row.notes = notes
            session.flush()

    def seed_sign_conventions_from_institutions(
        self,
        institution_mapping: dict[str, str],
        dry_run: bool = False,
    ) -> dict[str, int]:
        """Seed account_sign_conventions for every account_id in plaid_transactions.

        For each account_id in plaid_transactions, resolves its institution via
        plaid_items.institution_name (JOIN on item_id), falling back to the
        denormalized plaid_transactions.institution column. The 2026-06-11 prod
        rollout found the denormalized column ~94% NULL, so the JOIN is the
        primary source. Accounts whose institution is NULL or not in the map
        get the default 'expense_positive'.

        ON CONFLICT DO NOTHING — accounts that already have a row in
        account_sign_conventions (e.g., from a prior manual override via
        set_sign_convention) are preserved unchanged.

        All rows inserted carry provenance='seeded'.

        Institution names are matched verbatim (case-sensitive,
        whitespace-significant). New institutions are silently mapped to the
        default; verify by querying `account_sign_conventions WHERE
        provenance='seeded'` after seeding.

        Args:
            institution_mapping: dict mapping institution name -> sign_convention.
            dry_run: If True, performs all queries and classification but does
                not commit. Returns the same counts as a real run would produce.

        Returns:
            Counts: {"inserted": N, "skipped_existing": M, "default_applied": K}
            where 'default_applied' counts rows where institution was NULL or not
            in the map and the default was used.
        """
        session: Session = self._session_factory()
        try:
            # One row per account_id (GROUP BY prevents PK collisions when the
            # same account appears with differing institution values, e.g. NULL
            # on some rows). plaid_items.institution_name is the reliable
            # source; the denormalized column is the fallback.
            rows = (
                session.query(
                    PlaidTransaction.account_id,
                    func.max(
                        func.coalesce(
                            PlaidItem.institution_name,
                            PlaidTransaction.institution,
                        )
                    ).label("institution"),
                )
                .outerjoin(PlaidItem, PlaidItem.item_id == PlaidTransaction.item_id)
                .group_by(PlaidTransaction.account_id)
                .all()
            )
            pairs: list[tuple[str, str | None]] = [
                (str(row.account_id), row.institution) for row in rows
            ]

            # Batch existence check — one query for all candidate account_ids.
            account_ids = [account_id for account_id, _ in pairs]
            existing_ids: set[str] = set()
            if account_ids:
                existing_rows = (
                    session.query(AccountSignConvention.account_id)
                    .filter(AccountSignConvention.account_id.in_(account_ids))
                    .all()
                )
                existing_ids = {str(r.account_id) for r in existing_rows}

            inserted = 0
            skipped_existing = 0
            default_applied = 0

            for account_id, institution in pairs:
                convention: str
                if institution is not None and institution in institution_mapping:
                    convention = institution_mapping[institution]
                else:
                    convention = self._DEFAULT_SIGN_CONVENTION

                if account_id in existing_ids:
                    skipped_existing += 1
                    continue

                if institution is None or institution not in institution_mapping:
                    default_applied += 1

                notes = f"Seeded from institution={institution!r}"
                new_row = AccountSignConvention(
                    account_id=account_id,
                    sign_convention=convention,
                    provenance="seeded",
                    notes=notes,
                )
                session.add(new_row)
                inserted += 1

            if dry_run:
                session.rollback()
            else:
                session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        return {
            "inserted": inserted,
            "skipped_existing": skipped_existing,
            "default_applied": default_applied,
        }

    def has_sign_convention(self, account_id: str) -> bool:
        """Return True only if a real row exists for account_id.

        Unlike ``get_sign_convention``, this never returns a default — it checks
        whether the account_sign_conventions table has an entry for the given
        account_id.

        Args:
            account_id: The Plaid account_id to check.

        Returns:
            True if a row exists; False otherwise.
        """
        with self.session() as session:  # type: Session
            row = session.get(AccountSignConvention, account_id)
            return row is not None

    def list_sign_conventions(self) -> list[AccountSignConvention]:
        """List all sign convention rows ordered by (provenance, account_id).

        Returns:
            List of AccountSignConvention ORM instances, detached from session.
        """
        with self.session() as session:  # type: Session
            rows = (
                session.query(AccountSignConvention)
                .order_by(
                    AccountSignConvention.provenance.asc(),
                    AccountSignConvention.account_id.asc(),
                )
                .all()
            )
            for row in rows:
                session.expunge(row)
            return rows

    def list_plaid_transaction_ids_for_account(self, account_id: str) -> list[int]:
        """Return all plaid_transaction_id values for the given account_id.

        Args:
            account_id: The Plaid account_id to filter by.

        Returns:
            List of plaid_transaction_id integers for the account, ordered
            ascending.
        """
        with self.session() as session:  # type: Session
            rows = (
                session.query(PlaidTransaction.plaid_transaction_id)
                .filter(PlaidTransaction.account_id == account_id)
                .order_by(PlaidTransaction.plaid_transaction_id.asc())
                .all()
            )
            return [int(row.plaid_transaction_id) for row in rows]

    def delete_unverified_derived_by_plaid_ids(
        self, plaid_transaction_ids: list[int]
    ) -> int:
        """Delete only unverified derived_transactions for the given plaid IDs.

        Verified rows (is_verified=True) are preserved.

        Args:
            plaid_transaction_ids: Plaid transaction IDs whose unverified
                derived rows should be deleted.

        Returns:
            Count of rows deleted.
        """
        if not plaid_transaction_ids:
            return 0
        with self.session() as session:  # type: Session
            deleted = (
                session.query(DerivedTransaction)
                .filter(
                    DerivedTransaction.plaid_transaction_id.in_(plaid_transaction_ids),
                    DerivedTransaction.is_verified.is_(False),
                )
                .delete(synchronize_session=False)
            )
            session.flush()
            return int(deleted)

    # ------------------------------------------------------------------
    # Categorization read API (events, history, tags)
    #
    # All methods return plain JSON-serializable dicts/lists (never detached
    # ORM rows) so the categorizer agent's tools and the system-prompt
    # injection can use them after the session closes without risking
    # DetachedInstanceError.
    # ------------------------------------------------------------------

    @staticmethod
    def _category_event_to_dict(
        event: TransactionCategoryEvent,
        merchant_descriptor: str | None = None,
    ) -> dict[str, Any]:
        return {
            "event_id": event.event_id,
            "transaction_id": event.transaction_id,
            "merchant_descriptor": merchant_descriptor,
            "from_category_key": event.from_category_key,
            "to_category_key": event.to_category_key,
            "method": event.method,
            "model": event.model,
            "recategorization_reason": event.recategorization_reason,
            "categorization_reasoning": event.categorization_reasoning,
            "created_at": (event.created_at.isoformat() if event.created_at else None),
        }

    def recent_category_events(
        self, limit: int = 20, *, method: str | None = None
    ) -> list[dict[str, Any]]:
        """Return the most recent category-change events (newest first)."""
        with self.session() as session:  # type: Session
            query = (
                session.query(
                    TransactionCategoryEvent,
                    DerivedTransaction.merchant_descriptor,
                )
                .join(
                    DerivedTransaction,
                    TransactionCategoryEvent.transaction_id
                    == DerivedTransaction.transaction_id,
                )
                .order_by(
                    TransactionCategoryEvent.created_at.desc(),
                    TransactionCategoryEvent.event_id.desc(),
                )
            )
            if method is not None:
                query = query.filter(TransactionCategoryEvent.method == method)
            rows = query.limit(limit).all()
            return [
                self._category_event_to_dict(event, descriptor)
                for event, descriptor in rows
            ]

    def events_for_merchant(
        self, merchant_id: int, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Return category-change events for a merchant's transactions (newest first)."""
        with self.session() as session:  # type: Session
            query = (
                session.query(
                    TransactionCategoryEvent,
                    DerivedTransaction.merchant_descriptor,
                )
                .join(
                    DerivedTransaction,
                    TransactionCategoryEvent.transaction_id
                    == DerivedTransaction.transaction_id,
                )
                .filter(DerivedTransaction.merchant_id == merchant_id)
                .order_by(
                    TransactionCategoryEvent.created_at.desc(),
                    TransactionCategoryEvent.event_id.desc(),
                )
            )
            rows = query.limit(limit).all()
            return [
                self._category_event_to_dict(event, descriptor)
                for event, descriptor in rows
            ]

    def events_for_transaction(self, transaction_id: int) -> list[dict[str, Any]]:
        """Return the full category-change history of one transaction (oldest first)."""
        with self.session() as session:  # type: Session
            query = (
                session.query(TransactionCategoryEvent)
                .join(
                    DerivedTransaction,
                    TransactionCategoryEvent.transaction_id
                    == DerivedTransaction.transaction_id,
                )
                .filter(TransactionCategoryEvent.transaction_id == transaction_id)
                .order_by(
                    TransactionCategoryEvent.created_at.asc(),
                    TransactionCategoryEvent.event_id.asc(),
                )
            )
            events = query.all()
            return [self._category_event_to_dict(event) for event in events]

    def verified_category_for_descriptor(self, descriptor: str) -> str | None:
        """Most recent VERIFIED category key for an exact merchant descriptor.

        Powers the categorization fast path: an exact descriptor that already has
        a verified categorization is reused with confidence 1.0 and no LLM call.
        """
        if not descriptor:
            return None
        with self.session() as session:  # type: Session
            query = (
                session.query(Category.key)
                .join(
                    DerivedTransaction,
                    DerivedTransaction.category_id == Category.category_id,
                )
                .filter(
                    DerivedTransaction.merchant_descriptor == descriptor,
                    DerivedTransaction.is_verified.is_(True),
                )
                .order_by(
                    DerivedTransaction.category_assigned_at.desc(),
                    DerivedTransaction.transaction_id.desc(),
                )
            )
            row = query.first()
            return row[0] if row else None

    def get_transactions_by_merchant_descriptor(
        self, descriptor: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Recent transactions for an exact merchant descriptor (newest first)."""
        if not descriptor:
            return []
        with self.session() as session:  # type: Session
            query = (
                session.query(
                    DerivedTransaction.transaction_id,
                    DerivedTransaction.posted_at,
                    DerivedTransaction.amount_cents,
                    DerivedTransaction.is_verified,
                    Category.key,
                )
                .outerjoin(
                    Category, Category.category_id == DerivedTransaction.category_id
                )
                .filter(DerivedTransaction.merchant_descriptor == descriptor)
                .order_by(DerivedTransaction.posted_at.desc())
            )
            rows = query.limit(limit).all()
            return [
                {
                    "transaction_id": tid,
                    "date": posted_at.isoformat() if posted_at else None,
                    "amount": amount_cents / 100.0,
                    "is_verified": bool(is_verified),
                    "category_key": category_key,
                }
                for tid, posted_at, amount_cents, is_verified, category_key in rows
            ]

    def get_merchant_category_distribution(
        self, descriptor: str
    ) -> list[dict[str, Any]]:
        """How an exact merchant descriptor has been categorized historically.

        Returns ``[{category_key, count, verified_count}]`` sorted by count desc.
        """
        if not descriptor:
            return []
        with self.session() as session:  # type: Session
            query = (
                session.query(
                    Category.key,
                    DerivedTransaction.is_verified,
                    func.count().label("n"),
                )
                .join(
                    DerivedTransaction,
                    DerivedTransaction.category_id == Category.category_id,
                )
                .filter(DerivedTransaction.merchant_descriptor == descriptor)
                .group_by(Category.key, DerivedTransaction.is_verified)
            )
            rows = query.all()
        dist: dict[str, dict[str, Any]] = {}
        for category_key, is_verified, count in rows:
            entry = dist.setdefault(
                category_key,
                {"category_key": category_key, "count": 0, "verified_count": 0},
            )
            entry["count"] += count
            if is_verified:
                entry["verified_count"] += count
        return sorted(dist.values(), key=lambda e: e["count"], reverse=True)

    @contextmanager
    def try_advisory_lock(self, key: int) -> Iterator[bool]:
        """Best-effort run-level lock. Yields True if acquired, False if already held.

        Uses a Postgres session-level advisory lock (held on a dedicated connection
        for the duration of the ``with`` block). On SQLite (single-user dev) this is
        a no-op that always yields True.
        """
        if self._engine.dialect.name != "postgresql":
            yield True
            return
        conn = self._engine.connect()
        try:
            acquired = bool(
                conn.execute(
                    text("SELECT pg_try_advisory_lock(:k)"), {"k": key}
                ).scalar()
            )
            if not acquired:
                yield False
                return
            try:
                yield True
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
                conn.commit()
        finally:
            conn.close()

    def tags_for_transactions(self, transaction_ids: list[int]) -> dict[int, list[str]]:
        """Map each transaction id to its tag names (empty list if untagged)."""
        result: dict[int, list[str]] = {tid: [] for tid in transaction_ids}
        if not transaction_ids:
            return result
        with self.session() as session:  # type: Session
            rows = (
                session.query(TransactionTag.transaction_id, Tag.name)
                .join(Tag, Tag.tag_id == TransactionTag.tag_id)
                .filter(TransactionTag.transaction_id.in_(transaction_ids))
                .all()
            )
        for transaction_id, name in rows:
            result.setdefault(transaction_id, []).append(name)
        return result

    def get_transactions_by_tag(
        self, tag_name: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Recent transactions carrying a given tag (newest first)."""
        with self.session() as session:  # type: Session
            query = (
                session.query(
                    DerivedTransaction.transaction_id,
                    DerivedTransaction.merchant_descriptor,
                    DerivedTransaction.posted_at,
                    DerivedTransaction.amount_cents,
                    Category.key,
                )
                .join(
                    TransactionTag,
                    TransactionTag.transaction_id == DerivedTransaction.transaction_id,
                )
                .join(Tag, Tag.tag_id == TransactionTag.tag_id)
                .outerjoin(
                    Category, Category.category_id == DerivedTransaction.category_id
                )
                .filter(Tag.name == tag_name)
                .order_by(DerivedTransaction.posted_at.desc())
            )
            rows = query.limit(limit).all()
            return [
                {
                    "transaction_id": tid,
                    "merchant_descriptor": descriptor,
                    "date": posted_at.isoformat() if posted_at else None,
                    "amount": amount_cents / 100.0,
                    "category_key": category_key,
                }
                for tid, descriptor, posted_at, amount_cents, category_key in rows
            ]
