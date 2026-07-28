from logging.config import fileConfig
import os

from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool, text

from alembic import context

# Load environment variables from .env file
load_dotenv()

# Import the Base and all models for autogenerate support
# Import all models to ensure they're registered with Base
from penny.adapters.db.models import (  # noqa: F401, E402
    AccountSignConvention,
    Base,
    Category,
    DerivedTransaction,
    EmailReceipt,
    Merchant,
    PendingReceiptMatch,
    PlaidAccount,
    PlaidItem,
    PlaidTransaction,
    Tag,
    TransactionCategoryEvent,
    TransactionItem,
    TransactionTag,
)

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Resolve the target DB URL. An EXPLICIT sqlalchemy.url set on the config wins
# (e.g. `penny.schema.upgrade_to_head(url)` or `alembic -x`), so callers can
# migrate a chosen DB even when DATABASE_URL points elsewhere — otherwise a
# stray DATABASE_URL (a shell/.env pointing at prod) would silently override
# the requested target. The CLI/release path leaves sqlalchemy.url empty
# (alembic.ini default), so it falls back to DATABASE_URL as before.
database_url = config.get_main_option("sqlalchemy.url") or os.environ.get(
    "DATABASE_URL"
)

if database_url:
    config.set_main_option("sqlalchemy.url", database_url)

# add your model's MetaData object here
# for 'autogenerate' support
target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        # On a fresh Postgres, alembic would create alembic_version.version_num
        # as VARCHAR(32); this repo's descriptive revision ids exceed that, so
        # the chain would fail at 001. Pre-create the table wide (idempotent —
        # existing DBs keep theirs). SQLite ignores VARCHAR length, so it's a
        # no-op there.
        if connection.dialect.name == "postgresql":
            connection.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS alembic_version ("
                    "version_num VARCHAR(128) NOT NULL, "
                    "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
                )
            )
            connection.commit()

        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
