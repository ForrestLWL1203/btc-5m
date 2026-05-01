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

    assert {key: skips[key] for key in ("missing_quote", "ask_gap", "leading", "cap")} == {
        "missing_quote": 0,
        "ask_gap": 0,
        "leading": 0,
        "cap": 0,
    }
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


def test_backtest_candidate_scans_entry_timeout_until_first_valid_quote():
    rows = [
        {"ts": 1120.0, "src": "poly", "token": "up", "bid": 0.57, "ask": 0.58},
        {"ts": 1120.0, "src": "poly", "token": "down", "bid": 0.41, "ask": 0.42},
        {"ts": 1123.0, "src": "poly", "token": "up", "bid": 0.63, "ask": 0.64},
    ]
    candidate = Candidate(
        name="test",
        entry_elapsed_sec=120,
        entry_timeout_sec=5,
        min_leading_ask=0.60,
        stop_loss_trigger=None,
    )

    trades, _ = backtest_candidate([_window(rows, outcome="up")], candidate)

    assert len(trades) == 1
    assert trades[0].entry_ts == pytest.approx(1123.0)
    assert trades[0].entry_price == pytest.approx(0.64)


def test_backtest_candidate_scans_dynamic_band_with_btc_threshold():
    rows = [
        {"ts": 1000.0, "src": "binance", "price": 100000.0},
        {"ts": 1110.0, "src": "binance", "price": 100040.0},
        {"ts": 1120.0, "src": "binance", "price": 100070.0},
        {"ts": 1120.0, "src": "poly", "token": "up", "bid": 0.65, "ask": 0.66},
        {"ts": 1120.0, "src": "poly", "token": "down", "bid": 0.33, "ask": 0.34},
    ]
    candidate = Candidate(
        name="test",
        entry_elapsed_sec=120,
        entry_end_elapsed_sec=180,
        min_leading_ask=0.60,
        stop_loss_trigger=None,
        btc_direction_confirm=True,
        strong_move_pct=0.06,
    )

    trades, skips = backtest_candidate([_window(rows, outcome="up")], candidate)

    assert skips["btc_strength"] == 0
    assert len(trades) == 1
    assert trades[0].entry_ts == pytest.approx(1120.0)
    assert trades[0].side == "up"
    assert trades[0].btc_move_pct == pytest.approx(0.07)


def test_backtest_candidate_rejects_poly_lead_against_btc_direction():
    rows = [
        {"ts": 1000.0, "src": "binance", "price": 100000.0},
        {"ts": 1110.0, "src": "binance", "price": 100040.0},
        {"ts": 1120.0, "src": "binance", "price": 100070.0},
        {"ts": 1120.0, "src": "poly", "token": "up", "bid": 0.42, "ask": 0.43},
        {"ts": 1120.0, "src": "poly", "token": "down", "bid": 0.56, "ask": 0.57},
        {"ts": 1125.0, "src": "poly", "token": "down", "bid": 0.61, "ask": 0.62},
    ]
    candidate = Candidate(
        name="test",
        entry_elapsed_sec=120,
        entry_end_elapsed_sec=180,
        min_leading_ask=0.60,
        stop_loss_trigger=None,
        btc_direction_confirm=True,
        strong_move_pct=0.06,
    )

    trades, skips = backtest_candidate([_window(rows, outcome="down")], candidate)

    assert trades == []
    assert skips["btc_direction"] == 1


def test_backtest_candidate_applies_entry_and_stop_loss_tick_buffers():
    rows = [
        {"ts": 1120.0, "src": "poly", "token": "up", "bid": 0.69, "ask": 0.70},
        {"ts": 1120.0, "src": "poly", "token": "down", "bid": 0.29, "ask": 0.30},
        {"ts": 1245.0, "src": "poly", "token": "up", "bid": 0.34, "ask": 0.35},
    ]
    candidate = Candidate(
        name="test",
        entry_elapsed_sec=120,
        min_leading_ask=0.60,
        stop_loss_trigger=0.35,
        entry_buffer_ticks=5,
        stop_loss_buffer_ticks=5,
    )

    trades, _ = backtest_candidate([_window(rows, outcome="up")], candidate)

    assert len(trades) == 1
    assert trades[0].entry_price == pytest.approx(0.75)
    assert trades[0].exit_price == pytest.approx(0.29)


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

    assert candidate.stop_loss_start_remaining_sec == pytest.approx(55)
    assert candidate.stop_loss_end_remaining_sec == pytest.approx(40)
