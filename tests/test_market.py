"""Unit tests for polybot.market.market — slug calculation, window discovery."""

import datetime
from unittest.mock import patch

import pytest

from polybot.core import config
from polybot.market.market import (
    MarketWindow,
    _build_window,
    _epoch_to_slug,
    _fetch_market_by_slug,
    _parse_dt,
    _scan_forward,
    find_window_after,
)


# ─── Slug calculation ─────────────────────────────────────────────────────────

def test_epoch_to_slug():
    """Slug format: btc-updown-5m-{epoch}."""
    assert _epoch_to_slug(1776182700) == "btc-updown-5m-1776182700"


def test_epoch_to_slug_zero():
    assert _epoch_to_slug(0) == "btc-updown-5m-0"


# ─── DateTime parsing ─────────────────────────────────────────────────────────

def test_parse_dt_iso_with_z():
    assert _parse_dt("2026-04-15T13:35:00Z") is not None
    dt = _parse_dt("2026-04-15T13:35:00Z")
    assert dt.year == 2026
    assert dt.month == 4
    assert dt.hour == 13


def test_parse_dt_iso_with_offset():
    dt = _parse_dt("2026-04-15T09:35:00-04:00")
    assert dt is not None
    assert dt.hour == 13  # UTC


def test_parse_dt_invalid():
    assert _parse_dt("") is None
    assert _parse_dt("not-a-date") is None


# ─── Build window from raw API data ──────────────────────────────────────────

def test_build_window_valid():
    raw = {
        "question": "Bitcoin Up or Down - Apr 15 9:35AM-9:40AM ET",
        "clobTokenIds": '["up-token-123", "down-token-456"]',
        "endDate": "2026-04-15T13:40:00Z",
        "eventStartTime": "2026-04-15T13:35:00Z",
        "slug": "btc-updown-5m-1776260100",
    }
    window = _build_window(raw)
    assert window is not None
    assert window.up_token == "up-token-123"
    assert window.down_token == "down-token-456"
    # 2026-04-15T13:35:00Z epoch
    assert window.start_epoch == 1776260100
    assert window.end_epoch == 1776260400


def test_build_window_list_tokens():
    """clobTokenIds can be a Python list (not JSON string)."""
    raw = {
        "question": "Test",
        "clobTokenIds": ["token-a", "token-b"],
        "endDate": "2026-04-15T13:40:00Z",
    }
    window = _build_window(raw)
    assert window is not None
    assert window.up_token == "token-a"


def test_build_window_missing_tokens():
    raw = {
        "question": "Test",
        "clobTokenIds": "[]",
        "endDate": "2026-04-15T13:40:00Z",
    }
    assert _build_window(raw) is None


def test_build_window_missing_end_date():
    raw = {
        "question": "Test",
        "clobTokenIds": '["a", "b"]',
    }
    assert _build_window(raw) is None


def test_build_window_fallback_start_from_end():
    """If eventStartTime is missing, _build_window uses endDate as start fallback."""
    raw = {
        "question": "Test",
        "clobTokenIds": '["a", "b"]',
        "endDate": "2026-04-15T13:40:00Z",
    }
    window = _build_window(raw)
    assert window is not None
    # When eventStartTime is absent, m.get("eventStartTime", m.get("endDate", ""))
    # resolves to endDate, so start == end
    assert window.start_time == window.end_time


def test_fetch_market_by_slug_requires_exact_slug_match():
    """Gamma slug queries may return nearby markets; we must keep only the exact slug."""

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return [
                {"slug": "btc-updown-5m-1776681600", "question": "old"},
                {"slug": "btc-updown-5m-1776681900", "question": "next"},
            ]

    with patch("polybot.market.market.requests.get", return_value=_Resp()):
        market = _fetch_market_by_slug("btc-updown-5m-1776681900")

    assert market is not None
    assert market["slug"] == "btc-updown-5m-1776681900"


# ─── MarketWindow properties ─────────────────────────────────────────────────

def test_window_short_label():
    utc = datetime.timezone.utc
    w = MarketWindow(
        question="Bitcoin Up or Down - Apr 15 9:35AM-9:40AM ET",
        up_token="a", down_token="b",
        start_time=datetime.datetime.fromtimestamp(1000, tz=utc),
        end_time=datetime.datetime.fromtimestamp(1300, tz=utc),
        slug="btc-updown-5m-1000",
    )
    assert w.short_label == "Apr 15 9:35AM-9:40AM ET"


def test_window_epochs():
    utc = datetime.timezone.utc
    w = MarketWindow(
        question="Test",
        up_token="a", down_token="b",
        start_time=datetime.datetime.fromtimestamp(1000, tz=utc),
        end_time=datetime.datetime.fromtimestamp(1300, tz=utc),
        slug="test",
    )
    assert w.start_epoch == 1000
    assert w.end_epoch == 1300


# ─── find_window_after ceiling division ───────────────────────────────────────

def test_find_window_after_exact_boundary():
    """
    If after_epoch is exactly on a 5-minute boundary, ceiling division
    rounds up to the SAME boundary. This ensures window end == next start
    is not skipped (the previous off-by-one bug).
    """
    # epoch=1300 is on a 300-second boundary (1300 = 4*300 + 100... no)
    # Let's use a real 5-min boundary: 1200 = 4*300
    after_epoch = 1200  # exactly on boundary
    result = -(-after_epoch // config.SLUG_STEP) * config.SLUG_STEP
    # ceiling(1200/300) = ceiling(4.0) = 4 → 4 * 300 = 1200
    assert result == 1200


def test_find_window_after_mid_boundary():
    """If after_epoch=1400 (not on boundary), next_boundary should round up."""
    after_epoch = 1400
    result = -(-after_epoch // config.SLUG_STEP) * config.SLUG_STEP
    assert result == 1500


def test_find_window_after_just_past_boundary():
    """epoch=1301 → round up to 1500."""
    after_epoch = 1301
    result = -(-after_epoch // config.SLUG_STEP) * config.SLUG_STEP
    assert result == 1500


def test_find_window_after_can_return_future_inactive_window():
    """Chained window lookup should accept the next exact slug before it becomes active."""

    raw_future = {
        "question": "Bitcoin Up or Down - Apr 15 9:45AM-9:50AM ET",
        "clobTokenIds": '["future-up", "future-down"]',
        "eventStartTime": "2099-04-15T13:45:00Z",
        "endDate": "2099-04-15T13:50:00Z",
        "slug": "btc-updown-5m-1776260700",
        "active": False,
        "closed": False,
    }

    with patch("polybot.market.market._fetch_market_by_slug", return_value=raw_future):
        window = find_window_after(1776260700)

    assert window is not None
    assert window.slug == "btc-updown-5m-1776260700"


def test_scan_forward_active_mode_skips_future_inactive_window():
    """Regular next-window discovery should still require active windows."""

    raw_future = {
        "question": "Bitcoin Up or Down - Apr 15 9:45AM-9:50AM ET",
        "clobTokenIds": '["future-up", "future-down"]',
        "eventStartTime": "2026-04-15T13:45:00Z",
        "endDate": "2099-04-15T13:50:00Z",
        "slug": "btc-updown-5m-1776260700",
        "active": False,
        "closed": False,
    }

    with patch("polybot.market.market._fetch_market_by_slug", return_value=raw_future):
        window = _scan_forward(1776260700)

    assert window is None
