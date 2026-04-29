from unittest.mock import MagicMock, patch

import pytest

from polybot.trading.fak_quotes import cap_limited_depth_quote, stop_loss_bid_quote


def test_cap_limited_depth_quote_scans_entry_book_independently():
    ws = MagicMock()
    ws.get_latest_ask_levels_with_size.return_value = [
        (0.61, 0.1),
        (0.62, 0.1),
        (0.63, 2.0),
    ]
    ws.get_latest_best_ask.return_value = 0.61
    ws.get_latest_best_ask_age.return_value = 0.01

    with patch("polybot.trading.fak_quotes.get_tick_size", return_value=0.01):
        quote = cap_limited_depth_quote(
            ws,
            "token-1",
            amount=1.0,
            max_entry_price=0.75,
            min_entry_level=3,
        )

    assert quote.enough is True
    assert quote.price == pytest.approx(0.63)
    assert quote.price_hint == pytest.approx(0.64)
    assert quote.levels_used == 2


def test_stop_loss_bid_quote_scans_sell_book_independently():
    ws = MagicMock()
    ws.get_latest_bid_levels_with_size.return_value = [
        (0.38, 0.1),
        (0.37, 0.4),
        (0.36, 1.0),
    ]
    ws.get_latest_best_bid_age.return_value = 0.01

    with patch("polybot.trading.fak_quotes.get_tick_size", return_value=0.01):
        quote = stop_loss_bid_quote(
            ws,
            "token-1",
            shares=1.0,
            max_age_sec=1.0,
            min_sell_level=3,
            min_sell_price=0.20,
        )

    assert quote.enough is True
    assert quote.price == pytest.approx(0.36)
    assert quote.price_hint == pytest.approx(0.33)
    assert quote.levels_used == 2
