"""Drift guard: the models (``create_all``) and the migration chain
(``alembic upgrade head``) must describe the same schema.

Finance ``Base`` runs on SQLite — the chain is SQLite-clean, so a table+column
comparison is apples-to-apples. Catches "changed a model, forgot the migration"
(and vice versa).

See docs/superpowers/plans/2026-07-09-alembic-sole-authority-on-postgres.md.
"""

from __future__ import annotations

from sqlalchemy import Engine, create_engine, inspect

from penny.adapters.db.models import Base
from penny.schema import upgrade_to_head


def _schema(engine: Engine) -> dict[str, list[str]]:
    insp = inspect(engine)
    return {
        t: sorted(c["name"] for c in insp.get_columns(t))
        for t in insp.get_table_names()
    }


def test_finance_models_match_migrations(tmp_path):
    # The explicit URL wins over any ambient DATABASE_URL (env.py precedence).
    mig_url = f"sqlite:///{tmp_path / 'mig.db'}"
    upgrade_to_head(mig_url)
    migrated = _schema(create_engine(mig_url))
    migrated.pop("alembic_version", None)

    mdl_engine = create_engine(f"sqlite:///{tmp_path / 'mdl.db'}")
    Base.metadata.create_all(mdl_engine)
    models = _schema(mdl_engine)

    assert models == migrated, (
        "finance models drifted from the migration chain — a model change needs "
        "a migration (or vice versa).\n"
        f"  tables only in models:     {sorted(set(models) - set(migrated))}\n"
        f"  tables only in migrations: {sorted(set(migrated) - set(models))}\n"
        f"  column diffs: "
        + str(
            {
                t: {"models": models[t], "migrations": migrated[t]}
                for t in set(models) & set(migrated)
                if models[t] != migrated[t]
            }
        )
    )
