"""A-share OHLCV should use Eastmoney instead of Yahoo Finance."""

from __future__ import annotations

import http.client
import json
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
def test_eastmoney_klines_retries_transient_disconnect():
    from tradingagents.dataflows import eastmoney_stock as em

    payload = {
        "data": {
            "klines": [
                "2026-07-01,33.00,33.25,33.80,32.90,123456,410000000,2.70,1.22,0.40,3.50",
            ]
        }
    }
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(payload).encode("utf-8")

    with mock.patch.object(
        em,
        "urlopen",
        side_effect=[http.client.RemoteDisconnected("closed"), response],
    ) as urlopen:
        df = em._fetch_eastmoney_klines("002472.SZ", "2026-07-01", "2026-07-02")

    assert df.loc[pd.Timestamp("2026-07-01"), "Close"] == 33.25
    assert urlopen.call_count == 2


@pytest.mark.unit
def test_eastmoney_klines_raises_after_three_retries():
    from tradingagents.dataflows import eastmoney_stock as em
    from tradingagents.dataflows.errors import NoMarketDataError

    with mock.patch.object(
        em,
        "urlopen",
        side_effect=http.client.RemoteDisconnected("closed"),
    ) as urlopen:
        with pytest.raises(NoMarketDataError, match="Eastmoney error"):
            em._fetch_eastmoney_klines("002472.SZ", "2026-07-01", "2026-07-02")

    assert urlopen.call_count == 4


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
def test_get_stock_data_eastmoney_prefers_mx_data_when_api_key_configured(monkeypatch):
    from tradingagents.dataflows import eastmoney_stock as em

    frame = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-07-01", "2026-07-02"]),
            "Open": [33.0, 33.2],
            "High": [33.8, 34.5],
            "Low": [32.9, 33.1],
            "Close": [33.25, 34.1],
            "Adj Close": [33.25, 34.1],
            "Volume": [123456, 234567],
        }
    ).set_index("Date")

    monkeypatch.setenv("MX_APIKEY", "test-key")
    with mock.patch.object(
        em,
        "_fetch_mx_data_ohlcv",
        return_value=frame,
    ) as mx_data_fetch, mock.patch.object(em, "_fetch_eastmoney_klines") as public_fetch:
        result = em.get_stock_data_eastmoney("600276.SS", "2026-07-01", "2026-07-02")

    assert "# Total records: 2" in result
    assert "34.1" in result
    mx_data_fetch.assert_called_once_with("600276.SS", "2026-07-01", "2026-07-02")
    public_fetch.assert_not_called()


@pytest.mark.unit
def test_stockstats_load_ohlcv_routes_a_share_to_eastmoney_cache_loader(tmp_path):
    from tradingagents.dataflows import eastmoney_stock as em
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
        em,
        "load_eastmoney_ohlcv",
        return_value=frame,
    ) as eastmoney_loader, mock.patch.object(su.yf, "download") as yf_download:
        result = su.load_ohlcv("600276.SS", "2026-07-02")

    assert len(result) == 2
    eastmoney_loader.assert_called_once_with("600276.SS", "2026-07-02")
    yf_download.assert_not_called()


@pytest.mark.unit
def test_load_eastmoney_ohlcv_prefers_mx_data_when_api_key_configured(tmp_path, monkeypatch):
    from tradingagents.dataflows import eastmoney_stock as em
    from tradingagents.dataflows.config import set_config

    frame = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-07-01", "2026-07-02"]),
            "Open": [33.0, 33.2],
            "High": [33.8, 34.5],
            "Low": [32.9, 33.1],
            "Close": [33.25, 34.1],
            "Adj Close": [33.25, 34.1],
            "Volume": [123456, 234567],
        }
    )

    set_config({"data_cache_dir": str(tmp_path)})
    monkeypatch.setenv("MX_APIKEY", "test-key")
    with mock.patch.object(
        em,
        "_fetch_mx_data_ohlcv",
        return_value=frame.set_index("Date"),
    ) as mx_data_fetch, mock.patch.object(em, "_fetch_eastmoney_klines") as public_fetch:
        result = em.load_eastmoney_ohlcv("601138.SS", "2026-07-02")

    assert len(result) == 2
    assert result.iloc[-1]["Close"] == 34.1
    mx_data_fetch.assert_called_once()
    public_fetch.assert_not_called()


@pytest.mark.unit
def test_mx_data_response_parse_to_yfinance_like_frame():
    from tradingagents.dataflows import eastmoney_stock as em

    result = {
        "status": 0,
        "data": {
            "data": {
                "searchDataResultDTO": {
                    "dataTableDTOList": [
                        {
                            "entityName": "比亚迪",
                            "table": {
                                "headName": ["2026-07-01", "2026-07-02"],
                                "1": [33.0, 33.2],
                                "2": [33.8, 34.5],
                                "3": [32.9, 33.1],
                                "4": [33.25, 34.1],
                                "5": [123456, 234567],
                            },
                            "nameMap": {
                                "1": "开盘价",
                                "2": "最高价",
                                "3": "最低价",
                                "4": "收盘价",
                                "5": "成交量",
                            },
                        }
                    ]
                }
            }
        },
    }

    df = em._mxds_rows_to_dataframe(em._extract_mx_data_rows(result))

    assert list(df.columns) == ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
    assert df.loc[pd.Timestamp("2026-07-01"), "Open"] == 33.0
    assert df.loc[pd.Timestamp("2026-07-02"), "Close"] == 34.1
    assert df.loc[pd.Timestamp("2026-07-02"), "Volume"] == 234567


@pytest.mark.unit
def test_mxds_mcp_rows_parse_to_yfinance_like_frame():
    from tradingagents.dataflows.eastmoney_stock import _mxds_rows_to_dataframe

    df = _mxds_rows_to_dataframe(
        [
            {"date": "2026-07-01", "open": 33, "close": 33.25, "high": 33.8, "low": 32.9, "volume": 123456},
            {"日期": "2026-07-02", "开盘": "33.20", "收盘": "34.10", "最高": "34.50", "最低": "33.10", "成交量": "234567"},
        ]
    )

    assert list(df.columns) == ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
    assert df.loc[pd.Timestamp("2026-07-01"), "Close"] == 33.25
    assert df.loc[pd.Timestamp("2026-07-02"), "High"] == 34.5


@pytest.mark.unit
def test_mxds_tool_selection_prefers_a_share_finance_tool():
    from tradingagents.dataflows.eastmoney_stock import _choose_mxds_kline_tool

    tools = [
        {"name": "mx_macro_data", "description": "查询宏观经济、行业经济与历史指标数据", "inputSchema": {"properties": {"query": {}}}},
        {"name": "mx_ashare_finance_data", "description": "查询A股金融数据，覆盖行情与技术指标", "inputSchema": {"properties": {"query": {}}}},
    ]

    assert _choose_mxds_kline_tool(tools)["name"] == "mx_ashare_finance_data"


@pytest.mark.unit
def test_mxds_sheet_response_parse_to_yfinance_like_frame():
    from tradingagents.dataflows.eastmoney_stock import _extract_mxds_rows, _mxds_rows_to_dataframe

    result = {
        "content": [
            {
                "type": "text",
                "text": (
                    '{"data":[{"columns":["工业富联(601138.SH)","2026-07-02(日)","2026-07-01(日)"],'
                    '"items":[["最低价","63.63元","69.5元"],["最高价","67.47元","73.88元"],'
                    '["开盘价","67.46元","73元"],["收盘价","64.02元","70元"],'
                    '["成交量","1.936亿股","1.63亿股"]],"sheetName":"工业富联的最低价、最高价等"}]}'
                ),
            }
        ]
    }

    df = _mxds_rows_to_dataframe(_extract_mxds_rows(result))

    assert len(df) == 2
    assert df.loc[pd.Timestamp("2026-07-02"), "Open"] == 67.46
    assert df.loc[pd.Timestamp("2026-07-02"), "Low"] == 63.63
    assert df.loc[pd.Timestamp("2026-07-01"), "Volume"] == 163000000
