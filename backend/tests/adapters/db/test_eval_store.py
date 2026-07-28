"""Tests for the eval store (eval_runs + eval_items) facade methods.

Covers the storage half of the categorizer eval pipeline: recording a run + its
items, resolving the ingest high-water mark (cohort watermark), and the
created-at cohort selection.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from penny.adapters.db.facade import DB
from penny.adapters.db.models import EvalItem, EvalRun, PlaidTransaction


def _create_db(tmp_path: Path) -> DB:
    db = DB(f"sqlite:///{tmp_path / 'test.db'}")
    db.create_schema()
    return db


def _seed_txn(
    db: DB,
    *,
    external_id: str,
    created_at: datetime,
    reporting_mode: str | None = None,
) -> int:
    with db.session() as session:
        plaid = PlaidTransaction(
            external_id=f"plaid-{external_id}",
            source="PLAID",
            account_id="acct-1",
            item_id=None,
            posted_at=date(2026, 1, 10),
            amount_cents=5000,
            currency="USD",
        )
        session.add(plaid)
        session.flush()
        from penny.adapters.db.models import DerivedTransaction

        txn = DerivedTransaction(
            plaid_transaction_id=plaid.plaid_transaction_id,
            external_id=external_id,
            amount_cents=5000,
            posted_at=date(2026, 1, 10),
            merchant_descriptor=f"MERCHANT {external_id}",
            created_at=created_at,
            reporting_mode=reporting_mode,
        )
        session.add(txn)
        session.flush()
        return int(txn.transaction_id)


def test_cohort_and_sweep_skip_investment_trades(tmp_path: Path) -> None:
    db = _create_db(tmp_path)
    normal = _seed_txn(db, external_id="n", created_at=datetime(2026, 6, 1, 8, 0))
    incl = _seed_txn(
        db,
        external_id="i",
        created_at=datetime(2026, 6, 1, 9, 0),
        reporting_mode="DEFAULT_INCLUDE",
    )
    trade = _seed_txn(
        db,
        external_id="t",
        created_at=datetime(2026, 6, 1, 10, 0),
        reporting_mode="DEFAULT_EXCLUDE",
    )

    # Investment trades (DEFAULT_EXCLUDE) are skipped; regular + DEFAULT_INCLUDE stay.
    cohort = db.derived_ids_created_since(None)
    assert normal in cohort and incl in cohort
    assert trade not in cohort

    sweep = db.get_uncategorized_derived_ids()
    assert normal in sweep and incl in sweep
    assert trade not in sweep


def test_cohort_created_since_watermark(tmp_path: Path) -> None:
    db = _create_db(tmp_path)
    t_old = _seed_txn(db, external_id="old", created_at=datetime(2026, 6, 1, 8, 0))
    t_mid = _seed_txn(db, external_id="mid", created_at=datetime(2026, 6, 2, 8, 0))
    t_new = _seed_txn(db, external_id="new", created_at=datetime(2026, 6, 3, 8, 0))

    # No watermark yet -> the whole table, oldest first.
    assert db.derived_ids_created_since(None) == [t_old, t_mid, t_new]

    # After a watermark, only strictly-later rows.
    assert db.derived_ids_created_since(datetime(2026, 6, 2, 8, 0)) == [t_new]
    assert db.max_created_at_for_ids([t_old, t_mid, t_new]) == datetime(
        2026, 6, 3, 8, 0
    )


def test_record_run_items_and_watermark_roundtrip(tmp_path: Path) -> None:
    db = _create_db(tmp_path)
    tid = _seed_txn(db, external_id="a", created_at=datetime(2026, 6, 3, 8, 0))

    run_id = db.record_eval_run(
        run_at=datetime(2026, 6, 27, 0, 0),
        status="completed",
        cohort_size=1,
        cohort_max_created_at=datetime(2026, 6, 3, 8, 0),
        branch_name="eval/2026-06-27",
        version={"model": "gemini-3.6-flash", "prompt_version": "1"},
    )
    db.record_eval_items(
        run_id,
        [
            {
                "transaction_id": tid,
                "merchant_descriptor": "MERCHANT a",
                "legacy_key": "food_and_dining.restaurants",
                "agent_key": "food_and_dining.groceries",
                "agent_reasoning": "looks like a grocery run",
                "agent_confidence": 0.82,
                "method_at_eval_time": "agent",
                "trace_link": "https://lf/trace/abc",
            }
        ],
    )
    db.set_eval_run_fixture(run_id, "r2://bucket/eval/2026-06-27.sqlite.gz")

    # Watermark advances to the recorded cohort max.
    assert db.last_eval_watermark() == datetime(2026, 6, 3, 8, 0)

    with db.session() as session:
        run = session.get(EvalRun, run_id)
        assert run.status == "completed"
        assert run.model == "gemini-3.6-flash"
        assert run.r2_fixture_url == "r2://bucket/eval/2026-06-27.sqlite.gz"
        items = session.query(EvalItem).filter(EvalItem.eval_run_id == run_id).all()
        assert len(items) == 1
        assert items[0].legacy_key == "food_and_dining.restaurants"
        assert items[0].agent_key == "food_and_dining.groceries"
        assert items[0].method_at_eval_time == "agent"


def test_watermark_ignores_skipped_runs(tmp_path: Path) -> None:
    db = _create_db(tmp_path)
    db.record_eval_run(
        run_at=datetime(2026, 6, 27, 0, 0),
        status="completed",
        cohort_size=1,
        cohort_max_created_at=datetime(2026, 6, 3, 8, 0),
    )
    # A later skipped run must not move the watermark backward/forward.
    db.record_eval_run(
        run_at=datetime(2026, 6, 27, 12, 0),
        status="skipped_empty",
        cohort_size=0,
        cohort_max_created_at=None,
    )
    assert db.last_eval_watermark() == datetime(2026, 6, 3, 8, 0)


def test_version_stamp_is_best_effort() -> None:
    from penny.eval.version import version_stamp

    stamp = version_stamp()
    assert set(stamp) == {
        "model",
        "prompt_version",
        "harness_sha",
        "taxonomy_version",
        "rules_version",
    }
    # model always resolves (env override or default); others are best-effort.
    assert stamp["model"]
