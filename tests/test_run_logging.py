"""Tests for run.py logging setup."""

import importlib
import logging
import sys


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
