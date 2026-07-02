"""A-share OHLCV should use Eastmoney instead of Yahoo Finance."""

from __future__ import annotations

from unittest import mock

import pandas as pd
import pytest


@pytest.mark.unit
def test_eastmoney_klines_parse_to_yfinance_like_frame():
    from tradingagents.dataflows.eastmoney_stock import _klines_to_dataframe

    df = _klines_to_dataframe(
        [
            "2026-07-01,33.00,33.25,33.80,32.90,123456,410000000,2.70,1.22,0.40,3.50",
            "2026-07-02,33.20,34.10,34.50,33.10,234567,790000000,4.20,2.56,0.85,4.10",
        ]
    )

    assert list(df.columns) == ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
    assert df.index.name == "Date"
    assert df.loc[pd.Timestamp("2026-07-01"), "Open"] == 33.0
    assert df.loc[pd.Timestamp("2026-07-01"), "High"] == 33.8
    assert df.loc[pd.Timestamp("2026-07-01"), "Low"] == 32.9
    assert df.loc[pd.Timestamp("2026-07-01"), "Close"] == 33.25
    assert df.loc[pd.Timestamp("2026-07-02"), "Volume"] == 234567


@pytest.mark.unit
def test_yfinance_stock_data_routes_a_share_to_eastmoney():
    import tradingagents.dataflows.y_finance as yfin

    with mock.patch.object(
        yfin,
        "get_stock_data_eastmoney",
        return_value="EASTMONEY_DATA",
    ) as eastmoney, mock.patch.object(yfin.yf, "Ticker") as yf_ticker:
        result = yfin.get_YFin_data_online("600276.SS", "2026-07-01", "2026-07-02")

    assert result == "EASTMONEY_DATA"
    eastmoney.assert_called_once_with("600276.SS", "2026-07-01", "2026-07-02")
    yf_ticker.assert_not_called()


@pytest.mark.unit
def test_stockstats_load_ohlcv_routes_a_share_to_eastmoney_cache_loader(tmp_path):
    from tradingagents.dataflows import stockstats_utils as su
    from tradingagents.dataflows.config import set_config

    frame = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-07-01", "2026-07-02"]),
            "Open": [33.0, 33.2],
            "High": [33.8, 34.5],
            "Low": [32.9, 33.1],
            "Close": [33.25, 34.1],
            "Volume": [123456, 234567],
        }
    )

    set_config({"data_cache_dir": str(tmp_path)})
    with mock.patch.object(
        su,
        "load_eastmoney_ohlcv",
        return_value=frame,
    ) as eastmoney_loader, mock.patch.object(su.yf, "download") as yf_download:
        result = su.load_ohlcv("600276.SS", "2026-07-02")

    assert len(result) == 2
    eastmoney_loader.assert_called_once_with("600276.SS", "2026-07-02")
    yf_download.assert_not_called()
