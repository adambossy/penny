import pytest
from unittest.mock import patch

from scripts.run import run_categorizer, run_analyzer, run_pipeline


class TestRunCategorizer:
    @patch("scripts.run.PersistTool")
    @patch("scripts.run.Categorizer")
    @patch("scripts.run.CSVIngest")
    def test_csv_mode_constructs_and_runs_loop(self, mock_csv, mock_cat, mock_persist):
        mock_ingest = mock_csv.return_value
        mock_ingest.fetch_next_batch.side_effect = [
            [{"id": 1, "desc": "a"}],
            [{"id": 2, "desc": "b"}],
            [],
        ]
        mock_cat_inst = mock_cat.return_value
        mock_cat_inst.categorize.return_value = [{"id": 1, "cat": "food"}]
        mock_persist_inst = mock_persist.return_value

        run_categorizer(mode="csv", data_dir="/tmp/data", batch_size=10)

        mock_csv.assert_called_once_with(data_dir="/tmp/data")
        assert mock_ingest.fetch_next_batch.call_count == 3
        assert mock_cat_inst.categorize.call_count == 2
        assert mock_persist_inst.save_transactions.call_count == 2

    @patch("scripts.run.PersistTool")
    @patch("scripts.run.Categorizer")
    @patch("scripts.run.PlaidIngest")
    def test_plaid_mode_constructs_and_runs_loop(self, mock_plaid, mock_cat, mock_persist):
        mock_ingest = mock_plaid.return_value
        mock_ingest.fetch_next_batch.side_effect = [[{"id": 1}], []]
        mock_cat_inst = mock_cat.return_value
        mock_cat_inst.categorize.return_value = [{"id": 1, "cat": "food"}]
        mock_persist_inst = mock_persist.return_value

        run_categorizer(
            mode="plaid",
            account_ids=["acc1"],
            start_date="2024-01-01",
            end_date="2024-12-31",
            batch_size=5,
        )

        mock_plaid.assert_called_once_with(
            account_ids=["acc1"],
            start_date="2024-01-01",
            end_date="2024-12-31",
        )
        assert mock_ingest.fetch_next_batch.call_count == 2
        assert mock_cat_inst.categorize.call_count == 1
        assert mock_persist_inst.save_transactions.call_count == 1

    def test_unsupported_mode_raises(self):
        with pytest.raises(ValueError, match="Unsupported mode"):
            run_categorizer(mode="unknown")


class TestRunAnalyzer:
    @patch("scripts.run.AnalyzerTool")
    def test_with_questions(self, mock_anal):
        mock_inst = mock_anal.return_value
        mock_inst.answer.return_value = {"result": "ok"}
        captured = []
        with patch("builtins.print", side_effect=lambda x: captured.append(x)):
            run_analyzer(questions=["Q1", "Q2"])

        assert mock_inst.answer.call_count == 2
        mock_inst.answer.assert_any_call("Q1")
        mock_inst.answer.assert_any_call("Q2")
        assert len(captured) == 2

    def test_none_questions_raises(self):
        with pytest.raises(ValueError, match="questions"):
            run_analyzer(questions=None)


class TestRunPipeline:
    @patch("scripts.run.run_analyzer")
    @patch("scripts.run.run_categorizer")
    def test_composes_categorizer_then_analyzer(self, mock_cat, mock_anal):
        order = []
        mock_cat.side_effect = lambda **kw: order.append("cat")
        mock_anal.side_effect = lambda **kw: order.append("anal")

        run_pipeline(
            mode="csv",
            data_dir="/tmp",
            batch_size=50,
            confidence_threshold=0.8,
            questions=["Q1"],
        )

        assert order == ["cat", "anal"]
        mock_cat.assert_called_once_with(
            mode="csv",
            data_dir="/tmp",
            account_ids=None,
            start_date=None,
            end_date=None,
            batch_size=50,
            confidence_threshold=0.8,
        )
        mock_anal.assert_called_once_with(questions=["Q1"])
