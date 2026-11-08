# -*- coding: utf-8 -*-
"""CLI contract: ``--no-notify`` blocks every external publish, local outputs stay.

Parses real CLI arguments and runs the real ``run_full_analysis``; only the pipeline,
notifier and the Feishu document client are replaced, so nothing is ever published.
"""

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import main


def _config(**overrides):
    values = dict(
        refresh_stock_list=MagicMock(),
        single_stock_notify=False,
        merge_email_notification=False,
        market_review_enabled=False,
        market_review_region="cn",
        daily_market_context_enabled=False,
        analysis_delay=0,
        backtest_enabled=False,
        report_type="simple",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class NoNotifyPublishTest(unittest.TestCase):
    def run_cli(self, *flags, config=None):
        with patch.object(sys, "argv", ["main.py", "--stocks", "2330.TW", "--no-market-review", *flags]):
            args = main.parse_arguments()
        pipeline = MagicMock()
        pipeline.run.return_value = [
            SimpleNamespace(
                code="2330.TW", name="台積電", sentiment_score=60, operation_advice="持有",
                trend_prediction="震荡", get_emoji=lambda: "🟡",
            )
        ]
        pipeline._last_local_report_path = "reports/report.md"
        pipeline.notifier.generate_aggregate_report.return_value = "dashboard"
        with patch.object(main, "_refresh_stock_index_cache_for_analysis"), patch.object(
            main, "_compute_trading_day_filter", return_value=(["2330.TW"], "cn", False),
        ), patch("src.core.pipeline.StockAnalysisPipeline", return_value=pipeline), patch(
            "src.core.market_review.run_market_review",
        ), patch("src.feishu_doc.FeishuDocManager") as feishu_manager, patch.object(
            main, "_run_auto_backtest",
        ), patch.object(main, "record_analysis_run_marker"):
            feishu_manager.return_value.is_configured.return_value = True
            feishu_manager.return_value.create_daily_doc.return_value = "https://example.invalid/doc"
            result = main.run_full_analysis(config or _config(), args, ["2330.TW"])
        return result, pipeline, feishu_manager

    def test_no_notify_publishes_nothing_and_keeps_local_flow(self):
        result, pipeline, feishu_manager = self.run_cli("--no-notify")

        self.assertTrue(result)
        feishu_manager.assert_not_called()
        feishu_manager.return_value.create_daily_doc.assert_not_called()
        pipeline.notifier.send.assert_not_called()
        self.assertFalse(pipeline.run.call_args.kwargs["send_notification"])

    def test_no_notify_with_merged_push_publishes_nothing(self):
        result, pipeline, feishu_manager = self.run_cli(
            "--no-notify", config=_config(merge_email_notification=True)
        )

        self.assertTrue(result)
        feishu_manager.return_value.create_daily_doc.assert_not_called()
        pipeline.notifier.send.assert_not_called()

    def test_without_flag_keeps_feishu_doc_and_link_push(self):
        result, pipeline, feishu_manager = self.run_cli()

        self.assertTrue(result)
        feishu_manager.return_value.create_daily_doc.assert_called_once()
        pipeline.notifier.send.assert_called_once()
        self.assertIn("https://example.invalid/doc", pipeline.notifier.send.call_args.args[0])
        self.assertTrue(pipeline.run.call_args.kwargs["send_notification"])


if __name__ == "__main__":
    unittest.main()
