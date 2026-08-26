"""A-share OHLCV should use Eastmoney instead of Yahoo Finance."""

from __future__ import annotations

import http.client
import json
import time
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
def test_eastmoney_klines_does_not_retry_disconnect():
    from tradingagents.dataflows import eastmoney_stock as em
    from tradingagents.dataflows.errors import NoMarketDataError

    with mock.patch.object(
        em,
        "urlopen",
        side_effect=http.client.RemoteDisconnected("closed"),
    ) as urlopen:
        with pytest.raises(NoMarketDataError, match="Eastmoney error"):
            em._fetch_eastmoney_klines("002472.SZ", "2026-07-01", "2026-07-02")

    assert urlopen.call_count == 1


@pytest.mark.unit
def test_tencent_klines_parse_to_yfinance_like_frame():
    from tradingagents.dataflows.eastmoney_stock import _tencent_rows_to_dataframe

    df = _tencent_rows_to_dataframe(
        [
            ["2026-07-01", "33.00", "33.25", "33.80", "32.90", "1234.000"],
            ["2026-07-02", "33.20", "34.10", "34.50", "33.10", "2345.000"],
        ]
    )

    assert list(df.columns) == ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
    assert df.index.name == "Date"
    assert df.loc[pd.Timestamp("2026-07-01"), "Open"] == 33.0
    assert df.loc[pd.Timestamp("2026-07-01"), "High"] == 33.8
    assert df.loc[pd.Timestamp("2026-07-01"), "Low"] == 32.9
    assert df.loc[pd.Timestamp("2026-07-01"), "Close"] == 33.25
    assert df.loc[pd.Timestamp("2026-07-02"), "Volume"] == 2345


@pytest.mark.unit
def test_fetch_tencent_klines_reads_qfqday_and_filters_range():
    from tradingagents.dataflows import eastmoney_stock as em

    payload = {
        "code": 0,
        "data": {
            "sh603881": {
                "qfqday": [
                    ["2026-06-30", "27.00", "27.10", "27.50", "26.80", "1000"],
                    ["2026-07-01", "33.00", "33.25", "33.80", "32.90", "1234"],
                    ["2026-07-02", "33.20", "34.10", "34.50", "33.10", "2345"],
                ]
            }
        },
    }
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(payload).encode("utf-8")

    with mock.patch.object(em, "urlopen", return_value=response) as urlopen:
        df = em._fetch_tencent_klines("603881.SS", "2026-07-01", "2026-07-02")

    assert list(df.index) == [pd.Timestamp("2026-07-01"), pd.Timestamp("2026-07-02")]
    assert df.loc[pd.Timestamp("2026-07-02"), "Close"] == 34.1
    request = urlopen.call_args.args[0]
    assert "web.ifzq.gtimg.cn" in request.full_url
    assert "sh603881" in request.full_url
    assert "qfq" in request.full_url


@pytest.mark.unit
def test_fetch_tencent_klines_chunks_multi_year_range():
    from tradingagents.dataflows import eastmoney_stock as em

    def payload_for(rows):
        return {
            "code": 0,
            "data": {"sh600276": {"qfqday": rows}},
        }

    responses = [
        mock.MagicMock(),
        mock.MagicMock(),
    ]
    responses[0].__enter__.return_value.read.return_value = json.dumps(
        payload_for([["2025-12-31", "60.00", "60.50", "61.00", "59.50", "1000"]])
    ).encode("utf-8")
    responses[1].__enter__.return_value.read.return_value = json.dumps(
        payload_for([["2026-01-02", "60.60", "61.10", "61.50", "60.20", "1100"]])
    ).encode("utf-8")

    with mock.patch.object(em, "urlopen", side_effect=responses) as urlopen:
        df = em._fetch_tencent_klines("600276.SS", "2025-12-31", "2026-01-02")

    assert urlopen.call_count == 2
    assert list(df.index) == [pd.Timestamp("2025-12-31"), pd.Timestamp("2026-01-02")]
    urls = [call.args[0].full_url for call in urlopen.call_args_list]
    assert "2025-12-31" in urls[0]
    assert "2026-01-02" in urls[1]


@pytest.mark.unit
def test_preferred_ohlcv_falls_back_to_tencent_after_eastmoney_fails(monkeypatch):
    from tradingagents.dataflows import eastmoney_stock as em
    from tradingagents.dataflows.errors import NoMarketDataError

    frame = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-07-01", "2026-07-02"]),
            "Open": [33.0, 33.2],
            "High": [33.8, 34.5],
            "Low": [32.9, 33.1],
            "Close": [33.25, 34.1],
            "Adj Close": [33.25, 34.1],
            "Volume": [1234, 2345],
        }
    ).set_index("Date")

    monkeypatch.delenv("MX_APIKEY", raising=False)
    with mock.patch.object(
        em,
        "_fetch_eastmoney_klines",
        side_effect=NoMarketDataError("603881.SS", "603881.SS", "Eastmoney error"),
    ) as eastmoney_fetch, mock.patch.object(
        em,
        "_fetch_tencent_klines",
        return_value=frame,
    ) as tencent_fetch:
        result = em.get_stock_data_eastmoney("603881.SS", "2026-07-01", "2026-07-02")

    assert "# Total records: 2" in result
    assert "34.1" in result
    eastmoney_fetch.assert_called_once_with("603881.SS", "2026-07-01", "2026-07-02")
    tencent_fetch.assert_called_once_with("603881.SS", "2026-07-01", "2026-07-02")


@pytest.mark.unit
def test_preferred_ohlcv_uses_tencent_when_mx_and_eastmoney_fail(monkeypatch):
    from tradingagents.dataflows import eastmoney_stock as em
    from tradingagents.dataflows.errors import NoMarketDataError

    frame = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-07-01"]),
            "Open": [33.0],
            "High": [33.8],
            "Low": [32.9],
            "Close": [33.25],
            "Adj Close": [33.25],
            "Volume": [1234],
        }
    ).set_index("Date")

    monkeypatch.setenv("MX_APIKEY", "test-key")
    with mock.patch.object(
        em,
        "_fetch_mx_data_ohlcv",
        side_effect=NoMarketDataError("603881.SS", "603881.SS", "MX data API returned no OHLCV rows"),
    ), mock.patch.object(
        em,
        "_fetch_eastmoney_klines",
        side_effect=NoMarketDataError("603881.SS", "603881.SS", "Eastmoney error"),
    ), mock.patch.object(
        em,
        "_fetch_tencent_klines",
        return_value=frame,
    ) as tencent_fetch:
        result = em.get_stock_data_eastmoney("603881.SS", "2026-07-01", "2026-07-02")

    assert "33.25" in result
    tencent_fetch.assert_called_once()


@pytest.mark.unit
def test_get_cn_a_share_short_name_reads_eastmoney_f58():
    from tradingagents.dataflows import eastmoney_stock as em

    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(
        {"data": {"f58": "华勤技术"}}
    ).encode("utf-8")

    with mock.patch.object(em, "urlopen", return_value=response):
        assert em.get_cn_a_share_short_name("603296.SS") == "华勤技术"


@pytest.mark.unit
def test_get_cn_a_share_short_name_removes_temporary_exchange_prefix(monkeypatch):
    from tradingagents.dataflows import eastmoney_stock as em

    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(
        {"data": {"f58": "XD工业富联"}}
    ).encode("utf-8")

    monkeypatch.delenv("MX_APIKEY", raising=False)
    with mock.patch.object(em, "urlopen", return_value=response):
        assert em.get_cn_a_share_short_name("601138.SS") == "工业富联"


@pytest.mark.unit
def test_get_cn_a_share_short_name_prefers_mx_data_when_configured(monkeypatch):
    from tradingagents.dataflows import eastmoney_stock as em

    payload = {
        "status": 0,
        "data": {
            "data": {
                "searchDataResultDTO": {
                    "dataTableDTOList": [
                        {
                            "table": {
                                "ZQMC_f58_0": ["卧龙电驱"],
                                "headName": ["2026-08-03 20:20"],
                            },
                        }
                    ],
                },
            },
        },
    }
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(payload).encode("utf-8")

    monkeypatch.setenv("MX_APIKEY", "test-key")
    with mock.patch.object(em, "urlopen", return_value=response) as urlopen:
        assert em.get_cn_a_share_short_name("600580.SS") == "卧龙电驱"

    request = urlopen.call_args.args[0]
    assert request.full_url == em._MX_DATA_API_URL


@pytest.mark.unit
def test_get_cn_a_share_short_name_retries_transient_mx_disconnect(monkeypatch):
    from tradingagents.dataflows import eastmoney_stock as em

    monkeypatch.setenv("MX_MIN_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("MX_RETRY_BASE_DELAY_SECONDS", "0")
    payload = {
        "status": 0,
        "data": {
            "data": {
                "searchDataResultDTO": {
                    "dataTableDTOList": [{"table": {"ZQMC_f58_0": ["特发信息"]}}],
                },
            },
        },
    }
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(payload).encode("utf-8")

    monkeypatch.setenv("MX_APIKEY", "test-key")
    with mock.patch.object(
        em,
        "urlopen",
        side_effect=[http.client.RemoteDisconnected("closed"), response],
    ) as urlopen:
        assert em.get_cn_a_share_short_name("000070.SZ") == "特发信息"

    assert urlopen.call_count == 2


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


def _mx_throttled_response():
    payload = {
        "success": True,
        "status": 0,
        "code": 0,
        "message": "ok",
        "data": {"message": "操作过于频繁", "status": -1, "code": 503, "data": None},
    }
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(payload).encode("utf-8")
    return response


def _mx_ohlcv_response():
    payload = {
        "status": 0,
        "data": {
            "status": 0,
            "code": 0,
            "message": "OK",
            "data": {
                "searchDataResultDTO": {
                    "dataTableDTOList": [
                        {
                            "table": {
                                "headName": ["2026-07-01"],
                                "1": [33.0],
                                "2": [33.8],
                                "3": [32.9],
                                "4": [33.25],
                                "5": [123456],
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
            },
        },
    }
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(payload).encode("utf-8")
    return response


@pytest.mark.unit
def test_mx_data_throttle_error_is_reported_verbatim(monkeypatch):
    from tradingagents.dataflows import eastmoney_stock as em
    from tradingagents.dataflows.errors import NoMarketDataError

    monkeypatch.setenv("MX_APIKEY", "test-key")
    monkeypatch.setenv("MX_MIN_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("MX_RETRY_BASE_DELAY_SECONDS", "0")
    monkeypatch.setenv("MX_REQUEST_RETRIES", "2")

    with mock.patch.object(
        em,
        "urlopen",
        side_effect=[_mx_throttled_response() for _ in range(3)],
    ) as urlopen:
        with pytest.raises(NoMarketDataError, match="操作过于频繁"):
            em._fetch_mx_data_ohlcv("600276.SS", "2026-07-01", "2026-07-02")

    assert urlopen.call_count == 3


@pytest.mark.unit
def test_mx_data_retries_throttle_then_returns_rows(monkeypatch):
    from tradingagents.dataflows import eastmoney_stock as em

    monkeypatch.setenv("MX_APIKEY", "test-key")
    monkeypatch.setenv("MX_MIN_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("MX_RETRY_BASE_DELAY_SECONDS", "0")

    with mock.patch.object(
        em,
        "urlopen",
        side_effect=[_mx_throttled_response(), _mx_ohlcv_response()],
    ) as urlopen, mock.patch.object(em, "_assert_ohlcv_not_stale"):
        df = em._fetch_mx_data_ohlcv("600276.SS", "2026-07-01", "2026-07-01")

    assert urlopen.call_count == 2
    assert df.loc[pd.Timestamp("2026-07-01"), "Close"] == 33.25


@pytest.mark.unit
def test_mx_data_requests_never_overlap_across_threads(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from tradingagents.dataflows import eastmoney_stock as em

    monkeypatch.setenv("MX_APIKEY", "test-key")
    monkeypatch.setenv("MX_MIN_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("MX_RETRY_BASE_DELAY_SECONDS", "0")

    state = threading.Lock()
    in_flight = 0
    peak = 0

    def fake_urlopen(*_args, **_kwargs):
        nonlocal in_flight, peak
        with state:
            in_flight += 1
            peak = max(peak, in_flight)
        time.sleep(0.05)
        with state:
            in_flight -= 1
        return _mx_ohlcv_response()

    with mock.patch.object(em, "urlopen", side_effect=fake_urlopen), mock.patch.object(
        em, "_assert_ohlcv_not_stale"
    ):
        with ThreadPoolExecutor(max_workers=3) as pool:
            frames = list(
                pool.map(
                    lambda ticker: em._fetch_mx_data_ohlcv(ticker, "2026-07-01", "2026-07-01"),
                    ["002555.SZ", "600276.SS", "600498.SS"],
                )
            )

    assert peak == 1
    assert all(len(frame) == 1 for frame in frames)


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
