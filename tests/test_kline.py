"""Tests for polybot.predict.kline — Binance K-line fetcher."""

import pytest
from unittest.mock import patch, MagicMock
from polybot.predict.kline import KlineCandle, BinanceKlineFetcher
from polybot.market.series import MarketSeries


class TestKlineCandle:
    def test_create_candle(self):
        c = KlineCandle(open_time=1000, open=100.0, high=105.0, low=99.0, close=103.0, volume=50.0)
        assert c.close == 103.0
        assert c.volume == 50.0


class TestBinanceKlineFetcher:
    def test_symbol_btc(self):
        f = BinanceKlineFetcher(MarketSeries.from_known("btc-updown-5m"))
        assert f.symbol == "BTCUSDT"

    def test_symbol_eth(self):
        f = BinanceKlineFetcher(MarketSeries.from_known("eth-updown-5m"))
        assert f.symbol == "ETHUSDT"

    def test_interval_5m(self):
        f = BinanceKlineFetcher(MarketSeries.from_known("btc-updown-5m"))
        assert f.interval == "1m"
        assert f.limit == 60

    def test_interval_15m(self):
        f = BinanceKlineFetcher(MarketSeries.from_known("btc-updown-15m"))
        assert f.interval == "5m"
        assert f.limit == 48

    def test_interval_4h(self):
        f = BinanceKlineFetcher(MarketSeries.from_known("btc-updown-4h"))
        assert f.interval == "1h"
        assert f.limit == 24

    @patch("polybot.predict.kline.requests.get")
    def test_fetch_parses_klines(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = [
            [1000, "100.0", "105.0", "99.0", "103.0", "50.0", 1060000, "2600", 0, "10", "5000", "0"],
            [2000, "103.0", "108.0", "102.0", "107.0", "60.0", 2060000, "3600", 0, "12", "6000", "0"],
        ]
        mock_get.return_value = mock_resp

        f = BinanceKlineFetcher(MarketSeries.from_known("btc-updown-5m"))
        candles = f.fetch()

        assert len(candles) == 2
        assert candles[0].open == 100.0
        assert candles[0].close == 103.0
        assert candles[1].volume == 60.0

    @patch("polybot.predict.kline.requests.get")
    def test_fetch_network_error_returns_empty(self, mock_get):
        mock_get.side_effect = Exception("Network error")
        f = BinanceKlineFetcher(MarketSeries.from_known("btc-updown-5m"))
        candles = f.fetch()
        assert candles == []

    @patch("polybot.predict.kline.requests.get")
    def test_fetch_empty_response(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = []
        mock_get.return_value = mock_resp

        f = BinanceKlineFetcher(MarketSeries.from_known("btc-updown-5m"))
        assert f.fetch() == []
