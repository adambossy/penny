from typing import List, Optional, Sequence

from loguru import logger

from penny.tools._services.ingest import CSVIngest, PlaidIngest
from penny.tools._services.categorizer import Categorizer
from penny.tools._services.persist import PersistTool
from penny.tools._services.analyzer import AnalyzerTool


def run_categorizer(
    *,
    mode: str,
    data_dir: Optional[str] = None,
    account_ids: Optional[Sequence[str]] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    batch_size: int = 25,
    confidence_threshold: float = 0.70,
) -> None:
    if mode == "csv":
        ingest = CSVIngest(data_dir=data_dir)
    elif mode == "plaid":
        ingest = PlaidIngest(
            account_ids=account_ids,
            start_date=start_date,
            end_date=end_date,
        )
    else:
        raise ValueError(f"Unsupported mode: {mode}")

    categorizer = Categorizer(confidence_threshold=confidence_threshold)
    persist = PersistTool()

    while True:
        batch = ingest.fetch_next_batch(batch_size)
        if not batch:
            break
        categorized = categorizer.categorize(batch)
        persist.save_transactions(categorized)
        logger.info(f"Categorized and persisted {len(batch)} transactions.")


def run_analyzer(
    *,
    questions: Optional[List[str]] = None,
) -> None:
    if questions is None:
        raise ValueError("questions must be provided; interactive mode is not yet supported.")

    analyzer = AnalyzerTool()
    for q in questions:
        result = analyzer.answer(q)
        print(result)


def run_pipeline(
    *,
    mode: str,
    data_dir: Optional[str] = None,
    account_ids: Optional[Sequence[str]] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    batch_size: int = 25,
    confidence_threshold: float = 0.70,
    questions: Optional[List[str]] = None,
) -> None:
    run_categorizer(
        mode=mode,
        data_dir=data_dir,
        account_ids=account_ids,
        start_date=start_date,
        end_date=end_date,
        batch_size=batch_size,
        confidence_threshold=confidence_threshold,
    )
    run_analyzer(questions=questions)
