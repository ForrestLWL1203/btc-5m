"""Tests for run.py logging setup."""

import importlib
import logging
import sys


def test_setup_file_logging_creates_run_specific_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sys.modules.pop("run", None)
    run = importlib.import_module("run")

    run._setup_file_logging("btc-updown-5m", "run-test")
    logging.getLogger("test_run_logging").info("hello")

    assert (tmp_path / "log" / "btc-updown-5m_trade.log").exists()
    assert (tmp_path / "log" / "btc-updown-5m_trade.jsonl").exists()
    assert (tmp_path / "log" / "runs" / "run-test" / "btc-updown-5m_trade.log").exists()
    assert (tmp_path / "log" / "runs" / "run-test" / "btc-updown-5m_trade.jsonl").exists()
