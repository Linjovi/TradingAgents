"""A-share OHLCV fetchers: Eastmoney public klines, MX skill API, and Tencent Finance."""

from __future__ import annotations

import http.client
import json
import logging
import os
import re
from datetime import datetime
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

from .config import get_config
from .errors import NoMarketDataError
from .stockstats_utils import _assert_ohlcv_not_stale
from .symbol_utils import is_cn_a_share
from .utils import safe_ticker_component

logger = logging.getLogger(__name__)

_API_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
_QUOTE_API_URL = "https://push2.eastmoney.com/api/qt/stock/get"
_TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
_UA = "tradingagents/0.2 (+https://github.com/TauricResearch/TradingAgents)"
_MX_DATA_API_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw/query"
_MXDS_MCP_URL = "https://mxapi.eastmoney.com/mxds/mcp"
_MCP_PROTOCOL_VERSION = "2025-06-18"
_DATE_COLUMN_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_TEMPORARY_NAME_PREFIX_RE = re.compile(r"^(?:XD|XR|DR)(?=[\u4e00-\u9fff])")
_EASTMONEY_NAME_RETRIES = 3
_TENCENT_KLINE_MAX_COUNT = 2000


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


def _tencent_symbol(ticker: str) -> str:
    """Return Tencent quote symbol, e.g. 603881.SS -> sh603881."""
    code, _, suffix = ticker.strip().upper().partition(".")
    prefix = {"SS": "sh", "SZ": "sz", "BJ": "bj"}.get(suffix)
    if prefix is None:
        if code.startswith(("6", "9")):
            prefix = "sh"
        elif code.startswith(("4", "8")):
            prefix = "bj"
        else:
            prefix = "sz"
    return f"{prefix}{code}"


def _clean_cn_a_share_short_name(name: str | None) -> str | None:
    if not isinstance(name, str):
        return None
    cleaned = _TEMPORARY_NAME_PREFIX_RE.sub("", name.strip())
    return cleaned or None


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


def _tencent_rows_to_dataframe(rows: list) -> pd.DataFrame:
    """Convert Tencent qfqday rows ``[date, open, close, high, low, volume]``."""
    parsed = []
    for item in rows:
        if not isinstance(item, (list, tuple)) or len(item) < 6:
            continue
        try:
            date = pd.to_datetime(item[0], errors="raise")
            open_ = float(item[1])
            close = float(item[2])
            high = float(item[3])
            low = float(item[4])
            volume = int(float(item[5]))
        except (TypeError, ValueError):
            continue
        parsed.append(
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

    df = pd.DataFrame(parsed)
    if df.empty:
        return df
    df = df.sort_values("Date").drop_duplicates(subset=["Date"], keep="last")
    df = df.set_index("Date")
    df.index.name = "Date"
    return df[["Open", "High", "Low", "Close", "Adj Close", "Volume"]]


def _first_present(row: dict, names: tuple[str, ...]):
    for name in names:
        if name in row:
            return row[name]
    return None


def _parse_mxds_number(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if value is None:
        raise ValueError("missing numeric value")
    text = str(value).replace(",", "").strip()
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        raise ValueError(f"not a number: {value!r}")
    number = float(match.group(0))
    if "亿" in text:
        number *= 100_000_000
    elif "万" in text:
        number *= 10_000
    return number


def _mxds_sheet_to_rows(sheet: dict) -> list[dict]:
    columns = sheet.get("columns") or []
    items = sheet.get("items") or []
    if len(columns) < 2 or not isinstance(items, list):
        return []

    date_columns: list[tuple[int, pd.Timestamp]] = []
    for idx, column in enumerate(columns[1:], start=1):
        match = _DATE_COLUMN_RE.search(str(column))
        if match:
            date_columns.append((idx, pd.to_datetime(match.group(1), errors="raise")))
    if not date_columns:
        return []

    field_map = {
        "开盘": "Open",
        "开盘价": "Open",
        "最高": "High",
        "最高价": "High",
        "最低": "Low",
        "最低价": "Low",
        "收盘": "Close",
        "收盘价": "Close",
        "成交量": "Volume",
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "volume": "Volume",
    }
    rows_by_date = {
        date: {"Date": date}
        for _, date in date_columns
    }
    for item in items:
        if not isinstance(item, list) or len(item) < 2:
            continue
        label = str(item[0]).strip()
        field = field_map.get(label) or field_map.get(label.lower())
        if field is None:
            continue
        for idx, date in date_columns:
            if idx >= len(item):
                continue
            try:
                value = _parse_mxds_number(item[idx])
            except ValueError:
                continue
            rows_by_date[date][field] = int(value) if field == "Volume" else value

    rows = []
    for row in rows_by_date.values():
        if {"Open", "High", "Low", "Close", "Volume"} <= row.keys():
            row["Adj Close"] = row["Close"]
            rows.append(row)
    return rows


def _mxds_rows_to_dataframe(rows: list) -> pd.DataFrame:
    """Convert MXDS MCP OHLCV rows to a Yahoo-like OHLCV DataFrame."""
    normalized = []
    for row in rows:
        if isinstance(row, str):
            parsed = _klines_to_dataframe([row])
            if not parsed.empty:
                normalized.append(parsed.reset_index().iloc[0].to_dict())
            continue
        if not isinstance(row, dict):
            continue

        try:
            date = pd.to_datetime(
                _first_present(row, ("date", "Date", "trade_date", "tradeDate", "日期")),
                errors="raise",
            )
            open_ = _parse_mxds_number(_first_present(row, ("open", "Open", "开盘", "开盘价")))
            close = _parse_mxds_number(_first_present(row, ("close", "Close", "收盘", "收盘价", "latest")))
            high = _parse_mxds_number(_first_present(row, ("high", "High", "最高", "最高价")))
            low = _parse_mxds_number(_first_present(row, ("low", "Low", "最低", "最低价")))
            volume = int(_parse_mxds_number(_first_present(row, ("volume", "Volume", "成交量", "vol"))))
        except (TypeError, ValueError):
            continue
        normalized.append(
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

    df = pd.DataFrame(normalized)
    if df.empty:
        return df
    df = df.sort_values("Date").drop_duplicates(subset=["Date"], keep="last")
    df = df.set_index("Date")
    df.index.name = "Date"
    return df[["Open", "High", "Low", "Close", "Adj Close", "Volume"]]


def _parse_mcp_response_body(body: bytes) -> dict:
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return {}
    if text.startswith("data:"):
        chunks = []
        for line in text.splitlines():
            if line.startswith("data:"):
                chunks.append(line.removeprefix("data:").strip())
        text = "\n".join(chunks).strip()
    return json.loads(text)


def _mxds_mcp_request(
    method: str,
    params: dict | None,
    api_key: str,
    *,
    session_id: str | None = None,
    timeout: float = 20.0,
    expect_response: bool = True,
) -> tuple[dict, str | None]:
    request_id = 1 if expect_response else None
    body = {"jsonrpc": "2.0", "method": method}
    if request_id is not None:
        body["id"] = request_id
    if params is not None:
        body["params"] = params

    headers = {
        "User-Agent": _UA,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "em_api_key": api_key,
    }
    if session_id:
        headers["mcp-session-id"] = session_id

    req = Request(
        os.getenv("MXDS_MCP_URL", _MXDS_MCP_URL),
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urlopen(req, timeout=timeout) as resp:
        new_session_id = resp.headers.get("mcp-session-id") or session_id
        if resp.status == 202 or not expect_response:
            return {}, new_session_id
        payload = _parse_mcp_response_body(resp.read())
    if payload.get("error"):
        raise NoMarketDataError("MXDS", "MXDS", str(payload["error"]))
    return payload.get("result") or payload, new_session_id


def _mxds_initialize(api_key: str, timeout: float) -> str | None:
    result, session_id = _mxds_mcp_request(
        "initialize",
        {
            "protocolVersion": _MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "tradingagents", "version": "0.3.0"},
        },
        api_key,
        timeout=timeout,
    )
    session_id = session_id or result.get("sessionId")
    try:
        _mxds_mcp_request(
            "notifications/initialized",
            {},
            api_key,
            session_id=session_id,
            timeout=timeout,
            expect_response=False,
        )
    except Exception as exc:  # noqa: BLE001 - notification support varies by MCP gateway
        logger.debug("MXDS MCP initialized notification failed: %s", exc)
    return session_id


def _mxds_tool_score(tool: dict) -> int:
    text = " ".join(
        str(tool.get(key, "")) for key in ("name", "title", "description")
    ).lower()
    score = 0
    if "ashare" in text or "a股" in text:
        score += 10
    if "stock" in text or "股票" in text:
        score += 4
    for needle in ("kline", "k-line", "ohlcv", "历史", "k线", "日线", "行情"):
        if needle in text:
            score += 2
    if "宏观" in text or "macro" in text:
        score -= 6
    for needle in ("fund", "资金", "report", "研报", "news", "新闻"):
        if needle in text:
            score -= 2
    return score


def _choose_mxds_kline_tool(tools: list[dict]) -> dict:
    candidates = sorted(tools, key=_mxds_tool_score, reverse=True)
    if not candidates or _mxds_tool_score(candidates[0]) <= 0:
        raise NoMarketDataError("MXDS", "MXDS", "MXDS MCP has no recognizable K-line tool")
    return candidates[0]


def _format_mxds_date(date: str, compact: bool) -> str:
    parsed = datetime.strptime(date, "%Y-%m-%d")
    return parsed.strftime("%Y%m%d" if compact else "%Y-%m-%d")


def _mxds_query(symbol: str, start_date: str, end_date: str) -> str:
    code, _, suffix = symbol.strip().upper().partition(".")
    exchange = {"SS": "SH", "SZ": "SZ", "BJ": "BJ"}.get(suffix, suffix)
    display_symbol = f"{code}.{exchange}" if exchange else code
    return (
        f"查询A股{display_symbol}从{start_date}到{end_date}每个交易日的日K线数据，"
        "必须逐日返回日期、每日开盘价、每日最高价、每日最低价、每日收盘价、每日成交量"
    )


def _mx_data_short_name_query(ticker: str) -> str:
    code, _, suffix = ticker.strip().upper().partition(".")
    exchange = {"SS": "SH", "SZ": "SZ", "BJ": "BJ"}.get(suffix, suffix)
    display_symbol = f"{code}.{exchange}" if exchange else code
    return f"查询A股{display_symbol}的股票中文简称，只返回股票简称"


def _mxds_tool_arguments(tool: dict, symbol: str, start_date: str, end_date: str) -> dict:
    schema = tool.get("inputSchema") or tool.get("input_schema") or {}
    props = schema.get("properties") or {}
    args = {}
    code, _, suffix = symbol.strip().upper().partition(".")

    for name, meta in props.items():
        lowered = name.lower()
        description = str(meta.get("description", "")).lower() if isinstance(meta, dict) else ""
        compact_date = "yyyymmdd" in lowered or "yyyymmdd" in description or lowered in {"beg", "end"}
        if lowered == "query":
            args[name] = _mxds_query(symbol, start_date, end_date)
        elif lowered in {"symbol", "ticker"} or "股票代码" in description:
            args[name] = symbol.upper()
        elif lowered in {"code", "stock_code", "stockcode"}:
            args[name] = code
        elif lowered == "secid":
            args[name] = _secid(symbol)
        elif lowered in {"market", "exchange"}:
            args[name] = suffix or ("SH" if code.startswith("6") else "SZ")
        elif lowered in {"start", "start_date", "startdate", "beg", "begin_date", "begindate"}:
            args[name] = _format_mxds_date(start_date, compact_date)
        elif lowered in {"end", "end_date", "enddate"}:
            args[name] = _format_mxds_date(end_date, compact_date)
        elif lowered in {"period", "klt", "freq", "frequency"}:
            args[name] = 101
        elif lowered in {"adjust", "adjust_type", "fqt", "fq"}:
            args[name] = 1
        elif lowered in {"limit", "lmt", "count", "size"}:
            args[name] = 1000000

    return args


def _json_loads_maybe(value):
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return value
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def _is_mxds_row_like(value) -> bool:
    if isinstance(value, str):
        return bool(re.match(r"^\d{4}-\d{2}-\d{2},", value.strip()))
    if not isinstance(value, dict):
        return False
    lowered_keys = {str(key).lower() for key in value}
    return (
        {"date", "open", "close", "high", "low"} <= lowered_keys
        or {"日期", "开盘", "收盘", "最高", "最低"} <= set(value)
    )


def _extract_mxds_rows(value) -> list:
    value = _json_loads_maybe(value)
    if isinstance(value, list):
        if value and all(_is_mxds_row_like(item) for item in value):
            return value
        for item in value:
            rows = _extract_mxds_rows(item)
            if rows:
                return rows
    if isinstance(value, dict):
        if "columns" in value and "items" in value:
            return _mxds_sheet_to_rows(value)
        if value.get("type") == "text" and "text" in value:
            rows = _extract_mxds_rows(value["text"])
            if rows:
                return rows
        for key in ("klines", "kline", "rows", "data", "items", "result", "records", "list"):
            if key in value:
                rows = _extract_mxds_rows(value[key])
                if rows:
                    return rows
        if _is_mxds_row_like(value):
            return [value]
        if "content" in value:
            rows = _extract_mxds_rows(value["content"])
            if rows:
                return rows
    return []


def _mx_data_label(key: str, name_map: dict, code_map: dict) -> str:
    mapped = name_map.get(key)
    if mapped is None and key.isdigit():
        mapped = name_map.get(int(key))
    if mapped not in (None, ""):
        return str(mapped)
    mapped = code_map.get(key)
    if mapped not in (None, ""):
        return str(mapped)
    return key


def _mx_data_field(label: str) -> str | None:
    normalized = label.strip().lower()
    if "成交量" in normalized or normalized in {"volume", "vol"}:
        return "成交量"
    if "开盘" in normalized or normalized == "open":
        return "开盘"
    if "最高" in normalized or normalized == "high":
        return "最高"
    if "最低" in normalized or normalized == "low":
        return "最低"
    if "收盘" in normalized or normalized in {"close", "latest"}:
        return "收盘"
    return None


def _mx_data_code_map(block: dict) -> dict[str, str]:
    for key in ("returnCodeMap", "returnCodeNameMap", "codeMap"):
        value = block.get(key)
        if isinstance(value, dict):
            return {str(k): str(v) for k, v in value.items()}
    return {}


def _extract_mx_data_rows(result: dict) -> list[dict]:
    """Extract OHLCV-like rows from the MX data skill API response."""
    data = result.get("data") or {}
    inner_data = data.get("data") or {}
    search_result = inner_data.get("searchDataResultDTO") or {}
    dto_list = search_result.get("dataTableDTOList") or []
    rows: list[dict] = []

    for dto in dto_list:
        if not isinstance(dto, dict):
            continue
        table = dto.get("table") or {}
        if not isinstance(table, dict):
            continue
        dates = table.get("headName") or []
        if not isinstance(dates, list) or not dates:
            continue
        date_labels = [str(date)[:10] for date in dates]
        name_map = dto.get("nameMap") or {}
        if isinstance(name_map, list):
            name_map = {str(i): value for i, value in enumerate(name_map)}
        elif not isinstance(name_map, dict):
            name_map = {}
        code_map = _mx_data_code_map(dto)
        by_date = [{"日期": date} for date in date_labels]

        for key, values in table.items():
            if key == "headName":
                continue
            field = _mx_data_field(_mx_data_label(str(key), name_map, code_map))
            if field is None:
                continue
            if not isinstance(values, list):
                values = [values]
            for idx, value in enumerate(values[:len(by_date)]):
                by_date[idx][field] = value

        rows.extend(
            row
            for row in by_date
            if {"日期", "开盘", "收盘", "最高", "最低", "成交量"} <= set(row)
        )

    return rows


def _extract_mx_data_short_name(result: dict) -> str | None:
    data = result.get("data") or {}
    inner_data = data.get("data") or {}
    search_result = inner_data.get("searchDataResultDTO") or {}
    dto_list = search_result.get("dataTableDTOList") or []

    for dto in dto_list:
        if not isinstance(dto, dict):
            continue
        table = dto.get("table") or {}
        if not isinstance(table, dict):
            continue
        for key, values in table.items():
            if key == "headName":
                continue
            if not isinstance(values, list):
                values = [values]
            for value in values:
                if isinstance(value, str) and value.strip():
                    return _clean_cn_a_share_short_name(value)
    return None


def _fetch_mx_data_ohlcv(
    ticker: str,
    start_date: str,
    end_date: str,
    *,
    timeout: float = 30.0,
) -> pd.DataFrame:
    """Fetch daily A-share OHLCV through the Eastmoney MX data skill API."""
    api_key = os.getenv("MX_APIKEY", "").strip()
    if not api_key:
        raise NoMarketDataError(ticker, ticker, "MX data API is not configured; set MX_APIKEY")

    req = Request(
        os.getenv("MX_DATA_API_URL", _MX_DATA_API_URL),
        data=json.dumps({"toolQuery": _mxds_query(ticker, start_date, end_date)}).encode("utf-8"),
        headers={
            "User-Agent": _UA,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "apikey": api_key,
        },
        method="POST",
    )
    with urlopen(req, timeout=timeout) as resp:
        result = json.loads(resp.read().decode("utf-8", errors="replace"))

    status = result.get("status") if isinstance(result, dict) else None
    if status != 0:
        message = result.get("message", "unknown") if isinstance(result, dict) else "invalid response"
        raise NoMarketDataError(ticker, ticker, f"MX data API error {status}: {message}")

    rows = _extract_mx_data_rows(result)
    df = _mxds_rows_to_dataframe(rows)
    if df.empty:
        raise NoMarketDataError(ticker, ticker, "MX data API returned no OHLCV rows")
    _assert_ohlcv_not_stale(df, end_date, ticker, ticker)
    return df


def _fetch_mx_data_short_name(ticker: str, *, timeout: float = 30.0) -> str | None:
    api_key = os.getenv("MX_APIKEY", "").strip()
    if not api_key:
        return None

    req = Request(
        os.getenv("MX_DATA_API_URL", _MX_DATA_API_URL),
        data=json.dumps({"toolQuery": _mx_data_short_name_query(ticker)}).encode("utf-8"),
        headers={
            "User-Agent": _UA,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "apikey": api_key,
        },
        method="POST",
    )
    max_attempts = _EASTMONEY_NAME_RETRIES + 1
    for attempt in range(1, max_attempts + 1):
        try:
            with urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read().decode("utf-8", errors="replace"))
        except (HTTPError, OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
            logger.debug(
                "MX data name lookup failed for %s (attempt %s/%s): %s",
                ticker,
                attempt,
                max_attempts,
                exc,
            )
            continue

        status = result.get("status") if isinstance(result, dict) else None
        if status != 0:
            logger.debug("MX data name lookup returned status %s for %s", status, ticker)
            return None
        name = _extract_mx_data_short_name(result)
        if name:
            return name
    return None


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


def _extract_tencent_rows(payload: dict, symbol: str) -> list:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return []
    block = data.get(symbol) or {}
    if not isinstance(block, dict):
        return []
    rows = block.get("qfqday") or block.get("day") or []
    return rows if isinstance(rows, list) else []


def _tencent_year_windows(start_date: str, end_date: str) -> list[tuple[str, str]]:
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    windows = []
    for year in range(start.year, end.year + 1):
        window_start = start if year == start.year else datetime(year, 1, 1)
        window_end = end if year == end.year else datetime(year, 12, 31)
        windows.append((window_start.strftime("%Y-%m-%d"), window_end.strftime("%Y-%m-%d")))
    return windows


def _fetch_tencent_klines(
    ticker: str,
    start_date: str,
    end_date: str,
    *,
    timeout: float = 12.0,
) -> pd.DataFrame:
    """Fetch daily forward-adjusted A-share klines from Tencent Finance."""
    if not is_cn_a_share(ticker):
        raise NoMarketDataError(ticker, ticker, "Tencent OHLCV only supports A-share tickers")

    symbol = _tencent_symbol(ticker)
    frames: list[pd.DataFrame] = []
    for window_start, window_end in _tencent_year_windows(start_date, end_date):
        start_dt = datetime.strptime(window_start, "%Y-%m-%d")
        end_dt = datetime.strptime(window_end, "%Y-%m-%d")
        count = min(max((end_dt - start_dt).days + 10, 20), _TENCENT_KLINE_MAX_COUNT)
        param = f"{symbol},day,{window_start},{window_end},{count},qfq"
        req = Request(
            f"{_TENCENT_KLINE_URL}?{urlencode({'param': param})}",
            headers={
                "User-Agent": _UA,
                "Referer": "https://finance.qq.com/",
                "Accept": "application/json, text/plain, */*",
            },
        )
        try:
            with urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        except HTTPError as exc:
            logger.warning("Tencent OHLCV HTTP error %s for %s", exc.code, ticker)
            raise NoMarketDataError(ticker, ticker, f"Tencent HTTP {exc.code}") from exc
        except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
            logger.warning("Tencent OHLCV fetch failed for %s: %s", ticker, exc)
            raise NoMarketDataError(ticker, ticker, f"Tencent error: {exc}") from exc

        df = _tencent_rows_to_dataframe(_extract_tencent_rows(payload, symbol))
        if not df.empty:
            frames.append(df)

    if not frames:
        raise NoMarketDataError(ticker, ticker, f"Tencent returned no rows between {start_date} and {end_date}")

    data = pd.concat(frames).sort_index()
    data = data[~data.index.duplicated(keep="last")]
    start_ts = pd.Timestamp(start_date)
    end_ts = pd.Timestamp(end_date)
    data = data[(data.index >= start_ts) & (data.index <= end_ts)]
    if data.empty:
        raise NoMarketDataError(ticker, ticker, f"Tencent returned no rows between {start_date} and {end_date}")
    _assert_ohlcv_not_stale(data, end_date, ticker, ticker)
    return data


def get_cn_a_share_short_name(ticker: str, *, timeout: float = 8.0) -> str | None:
    """Return the Eastmoney Chinese short name for an A-share ticker."""
    if not is_cn_a_share(ticker):
        return None

    mx_name = _clean_cn_a_share_short_name(_fetch_mx_data_short_name(ticker))
    if mx_name:
        return mx_name

    params = {
        "secid": _secid(ticker),
        "fields": "f58",
    }
    req = Request(
        f"{_QUOTE_API_URL}?{urlencode(params)}",
        headers={
            "User-Agent": _UA,
            "Referer": "https://quote.eastmoney.com/",
            "Accept": "application/json, text/plain, */*",
        },
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (HTTPError, OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
        logger.debug("Eastmoney name lookup failed for %s: %s", ticker, exc)
        return None

    name = ((payload.get("data") or {}).get("f58") if isinstance(payload, dict) else None)
    return _clean_cn_a_share_short_name(name)


def _fetch_preferred_eastmoney_ohlcv(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """Fetch A-share OHLCV from MX, then Eastmoney, then Tencent."""
    if os.getenv("MX_APIKEY", "").strip():
        try:
            return _fetch_mx_data_ohlcv(symbol, start_date, end_date)
        except NoMarketDataError as exc:
            logger.warning("MX data OHLCV fetch failed for %s: %s", symbol, exc)
        except (HTTPError, OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
            logger.warning("MX data OHLCV fetch failed for %s: %s", symbol, exc)

    try:
        return _fetch_eastmoney_klines(symbol, start_date, end_date)
    except NoMarketDataError as exc:
        logger.warning(
            "Eastmoney OHLCV fetch failed for %s: %s; falling back to Tencent",
            symbol,
            exc,
        )

    return _fetch_tencent_klines(symbol, start_date, end_date)


def get_stock_data_eastmoney(symbol: str, start_date: str, end_date: str) -> str:
    """Return formatted A-share OHLCV data from Eastmoney."""
    data = _fetch_preferred_eastmoney_ohlcv(symbol, start_date, end_date)
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
        downloaded = _fetch_preferred_eastmoney_ohlcv(symbol, start_str, end_str).reset_index()
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
