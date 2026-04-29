from unittest.mock import AsyncMock, patch

import pytest

from polybot.trading.fak_execution import place_fak_buy, place_fak_stop_loss_sell
from polybot.trading.trading import OrderResult


@pytest.mark.asyncio
async def test_place_fak_buy_uses_shared_buy_gateway():
    expected = OrderResult(success=True, order_id="buy-1", filled_size=1.0, avg_price=0.62)

    with patch("polybot.trading.fak_execution.buy_token", new_callable=AsyncMock) as mock_buy:
        mock_buy.return_value = expected
        result = await place_fak_buy("token-1", 1.0, price_hint=0.62, price_hint_refresher=lambda: 0.63)

    assert result is expected
    assert mock_buy.await_args.args == ("token-1", 1.0)
    assert mock_buy.await_args.kwargs["price_hint"] == pytest.approx(0.62)


@pytest.mark.asyncio
async def test_place_fak_stop_loss_sell_uses_shared_sell_gateway():
    expected = OrderResult(success=True, order_id="sell-1", filled_size=1.0, avg_price=0.36)

    with patch("polybot.trading.fak_execution.sell_token", new_callable=AsyncMock) as mock_sell:
        mock_sell.return_value = expected
        result = await place_fak_stop_loss_sell(
            "token-1",
            1.0,
            price_hint=0.36,
            price_hint_refresher=lambda: 0.35,
            retry_count=3,
        )

    assert result is expected
    assert mock_sell.await_args.args == ("token-1", 1.0)
    assert mock_sell.await_args.kwargs["retry_count"] == 3
    assert mock_sell.await_args.kwargs["price_hint"] == pytest.approx(0.36)
