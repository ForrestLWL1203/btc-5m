import pytest

from tools.backtest_crowd_m1 import (
    Candidate,
    backtest_candidate,
    default_candidates,
    default_trade_candidate_names,
)


def _window(rows, *, outcome="up"):
    return (rows, {"ts": 1300.0, "window": "test-window", "direction": outcome})


def test_backtest_candidate_marks_false_stop_when_side_would_win():
    rows = [
        {"ts": 1119.0, "src": "poly", "token": "down", "bid": 0.29, "ask": 0.30},
        {"ts": 1120.0, "src": "poly", "token": "up", "bid": 0.69, "ask": 0.70},
        {"ts": 1245.0, "src": "poly", "token": "up", "bid": 0.34, "ask": 0.35},
    ]
    candidate = Candidate(
        name="test",
        entry_elapsed_sec=120,
        min_leading_ask=0.60,
        stop_loss_trigger=0.35,
    )

    trades, skips = backtest_candidate([_window(rows, outcome="up")], candidate)

    assert skips == {"missing_quote": 0, "ask_gap": 0, "leading": 0, "cap": 0}
    assert len(trades) == 1
    assert trades[0].exit_reason == "stop_loss"
    assert trades[0].false_stop is True
    assert trades[0].hold_pnl == pytest.approx((1.0 - 0.70) / 0.70)
    assert trades[0].realized_pnl == pytest.approx((0.34 - 0.70) / 0.70)


def test_backtest_candidate_ignores_stop_bid_below_min_sell_price():
    rows = [
        {"ts": 1119.0, "src": "poly", "token": "down", "bid": 0.29, "ask": 0.30},
        {"ts": 1120.0, "src": "poly", "token": "up", "bid": 0.69, "ask": 0.70},
        {"ts": 1245.0, "src": "poly", "token": "up", "bid": 0.19, "ask": 0.20},
    ]
    candidate = Candidate(
        name="test",
        entry_elapsed_sec=120,
        min_leading_ask=0.60,
        stop_loss_trigger=0.35,
        min_sell_price=0.20,
    )

    trades, _ = backtest_candidate([_window(rows, outcome="down")], candidate)

    assert len(trades) == 1
    assert trades[0].exit_reason == "settlement"
    assert trades[0].false_stop is False
    assert trades[0].realized_pnl == pytest.approx(-1.0)


def test_default_trade_candidates_cover_baseline_and_live_comparison_set():
    names = default_trade_candidate_names()

    assert "baseline_090_l060_sl035" in names
    assert "live_120_l066_slnone" in names
    assert "live_150_l068_sl035" in names
    assert "live_180_l062_sl035" in names


def test_default_candidate_names_use_rounded_threshold_labels():
    names = {candidate.name for candidate in default_candidates()}

    assert "live_120_l058_slnone" in names
    assert "live_120_l057_slnone" not in names


def test_default_candidates_use_current_stop_loss_window():
    candidate = default_candidates()[0]

    assert candidate.stop_loss_start_remaining_sec == pytest.approx(60)
    assert candidate.stop_loss_end_remaining_sec == pytest.approx(45)
