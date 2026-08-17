"""A-share get_news must route to Eastmoney, not Yahoo Finance."""

from unittest import mock

import pytest


@pytest.mark.unit
def test_get_news_yfinance_routes_a_share_to_eastmoney():
    import tradingagents.dataflows.yfinance_news as ynews

    with mock.patch.object(
        ynews,
        "get_news_eastmoney",
        return_value="EASTMONEY_NEWS",
    ) as eastmoney, mock.patch.object(ynews.yf, "Ticker") as yf_ticker:
        result = ynews.get_news_yfinance("002920.SZ", "2026-08-01", "2026-08-17")

    assert result == "EASTMONEY_NEWS"
    eastmoney.assert_called_once_with("002920.SZ", "2026-08-01", "2026-08-17")
    yf_ticker.assert_not_called()


@pytest.mark.unit
def test_get_news_yfinance_keeps_us_ticker_on_yahoo():
    import tradingagents.dataflows.yfinance_news as ynews

    fake_stock = mock.Mock()
    fake_stock.get_news.return_value = []

    with mock.patch.object(
        ynews,
        "get_news_eastmoney",
    ) as eastmoney, mock.patch.object(ynews.yf, "Ticker", return_value=fake_stock) as yf_ticker, mock.patch.object(
        ynews, "yf_retry", side_effect=lambda fn: fn()
    ):
        result = ynews.get_news_yfinance("AAPL", "2026-08-01", "2026-08-17")

    assert "No news found for AAPL" in result
    eastmoney.assert_not_called()
    yf_ticker.assert_called_once()
