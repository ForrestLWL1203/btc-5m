"""Tests for collector summary formatting."""

from tools.collect_data import DataCollector, WindowSummary


def test_print_summary_line_handles_missing_quotes(capsys):
    collector = DataCollector.__new__(DataCollector)
    collector._summary = WindowSummary(
        window_label="test-window",
        btc_start=100.0,
        btc_end=101.0,
        up_start=0.50,
        up_end=None,
        down_start=None,
        down_end=0.40,
        actual_direction="up",
        btc_ticks=10,
        poly_updates=20,
    )

    collector._print_summary_line()

    out = capsys.readouterr().out
    assert "BTC=+1.000%" in out
    assert "UP=0.500->n/a" in out
    assert "DOWN=n/a->0.400" in out
