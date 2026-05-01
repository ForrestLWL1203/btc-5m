"""Tests for run.py logging setup."""

import importlib
import logging
import sys

from polybot.config_loader import build_strategy
from polybot.market.series import MarketSeries
from polybot.trade_config import TradeConfig


def test_setup_file_logging_creates_only_run_specific_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sys.modules.pop("run", None)
    run = importlib.import_module("run")

    run._setup_file_logging("btc-updown-5m", "run-test")
    logging.getLogger("test_run_logging").info("hello")
    logging.getLogger("test_run_logging").warning("bad")

    trade_log = tmp_path / "log" / "runs" / "run-test" / "btc-updown-5m_trade.jsonl"
    error_log = tmp_path / "log" / "runs" / "run-test" / "btc-updown-5m_error.jsonl"
    assert trade_log.exists()
    assert error_log.exists()
    assert "hello" in trade_log.read_text(encoding="utf-8")
    assert "bad" not in trade_log.read_text(encoding="utf-8")
    assert "bad" in error_log.read_text(encoding="utf-8")
    assert "hello" not in error_log.read_text(encoding="utf-8")
    assert not (tmp_path / "log" / "btc-updown-5m_trade.jsonl").exists()
    assert not (tmp_path / "log" / "btc-updown-5m_trade.log").exists()
    assert not (tmp_path / "log" / "runs" / "run-test" / "btc-updown-5m_trade.log").exists()


def test_setup_file_logging_honors_run_dir_override(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    custom_run_dir = tmp_path / "remote-run"
    monkeypatch.setenv("POLYBOT_RUN_DIR", str(custom_run_dir))
    sys.modules.pop("run", None)
    run = importlib.import_module("run")

    run._setup_file_logging("btc-updown-5m", "run-test")
    logging.getLogger("test_run_logging").info("hello")

    assert (custom_run_dir / "btc-updown-5m_trade.jsonl").exists()
    assert (custom_run_dir / "btc-updown-5m_error.jsonl").exists()
    assert not (tmp_path / "log" / "btc-updown-5m_trade.jsonl").exists()
    assert not (tmp_path / "log" / "runs" / "run-test" / "btc-updown-5m_trade.jsonl").exists()


def test_setup_file_logging_removes_historical_logs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old_run_dir = tmp_path / "log" / "runs" / "old-run"
    old_run_dir.mkdir(parents=True)
    (old_run_dir / "btc-updown-5m_trade.jsonl").write_text("old\n", encoding="utf-8")
    (old_run_dir / "btc-updown-5m_error.jsonl").write_text("old\n", encoding="utf-8")
    (tmp_path / "log" / "btc-updown-5m_trade.jsonl").write_text("old\n", encoding="utf-8")
    (tmp_path / "log" / "btc-updown-5m_error.jsonl").write_text("old\n", encoding="utf-8")
    (tmp_path / "log" / "btc-updown-5m_trade.log").write_text("old\n", encoding="utf-8")
    sys.modules.pop("run", None)
    run = importlib.import_module("run")

    run._setup_file_logging("btc-updown-5m", "new-run")
    logging.getLogger("test_run_logging").info("new")

    assert (tmp_path / "log" / "runs" / "new-run" / "btc-updown-5m_trade.jsonl").exists()
    assert (tmp_path / "log" / "runs" / "new-run" / "btc-updown-5m_error.jsonl").exists()
    assert not old_run_dir.exists()
    assert not (tmp_path / "log" / "btc-updown-5m_trade.jsonl").exists()
    assert not (tmp_path / "log" / "btc-updown-5m_error.jsonl").exists()
    assert not (tmp_path / "log" / "btc-updown-5m_trade.log").exists()


def test_log_strategy_params_handles_crowd_without_removed_persistence_fields(caplog):
    sys.modules.pop("run", None)
    run = importlib.import_module("run")
    series = MarketSeries.from_known("btc-updown-5m")
    strategy = build_strategy(
        {
            "strategy": {
                "type": "crowd_m1",
                "entry_start_elapsed_sec": 45,
                "entry_end_elapsed_sec": 90,
                "strong_move_pct": 0.04,
            }
        },
        series,
    )
    trade_config = TradeConfig(
        amount=1.0,
        max_slippage_from_best_ask=0.04,
        stop_loss_enabled=True,
        stop_loss_start_remaining_sec=55,
        stop_loss_end_remaining_sec=40,
    )

    with caplog.at_level(logging.INFO, logger="run"):
        run._log_strategy_params(strategy, trade_config, series)

    assert "crowd_m1 entry_band=45s-90s" in caplog.text
    assert "persistence=" not in caplog.text
    assert "min_move_ratio" not in caplog.text
