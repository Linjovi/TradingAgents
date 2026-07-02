"""Eastmoney OHLCV fetcher for Chinese A-shares.

Yahoo Finance often rate-limits A-share historical price calls. Eastmoney's
public K-line endpoint is a better default for SSE/SZSE/BSE equities while
leaving other markets on the existing Yahoo path.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
from datetime import datetime
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd
from dateutil.relativedelta import relativedelta

from .config import get_config
from .errors import NoMarketDataError
from .stockstats_utils import _assert_ohlcv_not_stale
from .symbol_utils import is_cn_a_share
from .utils import safe_ticker_component

logger = logging.getLogger(__name__)

_API_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
_UA = "tradingagents/0.2 (+https://github.com/TauricResearch/TradingAgents)"


def _secid(ticker: str) -> str:
    """Return Eastmoney secid for an A-share ticker."""
    code, _, suffix = ticker.strip().upper().partition(".")
    if suffix == "SS":
        market = "1"
    elif suffix in {"SZ", "BJ"}:
        market = "0"
    elif code.startswith("6"):
        market = "1"
    else:
        market = "0"
    return f"{market}.{code}"


def _klines_to_dataframe(klines: list[str]) -> pd.DataFrame:
    """Convert Eastmoney kline strings to a Yahoo-like OHLCV DataFrame."""
    rows = []
    for item in klines:
        parts = item.split(",")
        if len(parts) < 6:
            continue
        try:
            date = pd.to_datetime(parts[0], errors="raise")
            open_ = float(parts[1])
            close = float(parts[2])
            high = float(parts[3])
            low = float(parts[4])
            volume = int(float(parts[5]))
        except (TypeError, ValueError):
            continue
        rows.append(
            {
                "Date": date,
                "Open": open_,
                "High": high,
                "Low": low,
                "Close": close,
                "Adj Close": close,
                "Volume": volume,
            }
        )

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.sort_values("Date").drop_duplicates(subset=["Date"], keep="last")
    df = df.set_index("Date")
    df.index.name = "Date"
    return df[["Open", "High", "Low", "Close", "Adj Close", "Volume"]]


def _fetch_eastmoney_klines(
    ticker: str,
    start_date: str,
    end_date: str,
    *,
    timeout: float = 12.0,
) -> pd.DataFrame:
    """Fetch daily A-share klines from Eastmoney as a DataFrame."""
    if not is_cn_a_share(ticker):
        raise NoMarketDataError(ticker, ticker, "Eastmoney OHLCV only supports A-share tickers")

    start = datetime.strptime(start_date, "%Y-%m-%d").strftime("%Y%m%d")
    end = datetime.strptime(end_date, "%Y-%m-%d").strftime("%Y%m%d")
    params = {
        "secid": _secid(ticker),
        "fields1": "f1,f2,f3,f4,f5,f6",
        # f51=date,f52=open,f53=close,f54=high,f55=low,f56=volume,...
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",  # daily
        "fqt": "1",  # forward adjusted
        "beg": start,
        "end": end,
        "lmt": "1000000",
    }
    req = Request(
        f"{_API_URL}?{urlencode(params)}",
        headers={
            "User-Agent": _UA,
            "Referer": "https://quote.eastmoney.com/",
            "Accept": "application/json, text/plain, */*",
        },
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except HTTPError as exc:
        logger.warning("Eastmoney OHLCV HTTP error %s for %s", exc.code, ticker)
        raise NoMarketDataError(ticker, ticker, f"Eastmoney HTTP {exc.code}") from exc
    except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
        logger.warning("Eastmoney OHLCV fetch failed for %s: %s", ticker, exc)
        raise NoMarketDataError(ticker, ticker, f"Eastmoney error: {exc}") from exc

    klines = ((payload.get("data") or {}).get("klines") or []) if isinstance(payload, dict) else []
    df = _klines_to_dataframe(klines)
    if df.empty:
        raise NoMarketDataError(ticker, ticker, f"Eastmoney returned no rows between {start_date} and {end_date}")
    _assert_ohlcv_not_stale(df, end_date, ticker, ticker)
    return df


def get_stock_data_eastmoney(symbol: str, start_date: str, end_date: str) -> str:
    """Return formatted A-share OHLCV data from Eastmoney."""
    data = _fetch_eastmoney_klines(symbol, start_date, end_date)
    data = data.round({"Open": 2, "High": 2, "Low": 2, "Close": 2, "Adj Close": 2})

    header = f"# Stock data for {symbol.upper()} from {start_date} to {end_date} (source: Eastmoney)\n"
    header += f"# Total records: {len(data)}\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return header + data.to_csv()


def load_eastmoney_ohlcv(symbol: str, curr_date: str) -> pd.DataFrame:
    """Fetch/cache five years of Eastmoney OHLCV data for stockstats."""
    config = get_config()
    curr_date_dt = pd.to_datetime(curr_date)
    today = pd.Timestamp.today()
    start_date = today - pd.DateOffset(years=5)
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = today.strftime("%Y-%m-%d")

    os.makedirs(config["data_cache_dir"], exist_ok=True)
    safe_symbol = safe_ticker_component(symbol.upper())
    data_file = os.path.join(
        config["data_cache_dir"],
        f"{safe_symbol}-Eastmoney-data-{start_str}-{end_str}.csv",
    )

    data = None
    if os.path.exists(data_file):
        cached = pd.read_csv(data_file, on_bad_lines="skip", encoding="utf-8")
        if not cached.empty and "Close" in cached.columns:
            data = cached

    if data is None:
        downloaded = _fetch_eastmoney_klines(symbol, start_str, end_str).reset_index()
        downloaded.to_csv(data_file, index=False, encoding="utf-8")
        data = downloaded

    data["Date"] = pd.to_datetime(data["Date"], errors="coerce")
    data = data.dropna(subset=["Date", "Close"])
    price_cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in data.columns]
    data[price_cols] = data[price_cols].apply(pd.to_numeric, errors="coerce")
    data[price_cols] = data[price_cols].ffill().bfill()
    data = data[data["Date"] <= curr_date_dt]
    _assert_ohlcv_not_stale(data, curr_date, symbol, symbol)
    return data
