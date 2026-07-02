"""东方财富网股票新闻抓取器 — A股本地化新闻数据

替代 Yahoo Finance 的 ``get_news()``，专门为 A 股提供中文新闻。
东方财富（eastmoney.com）是中国最大的财经信息平台，对 A 股资讯覆盖全面，
包括公告、研究报告、市场资讯等。

数据来源：
  ``https://np-listapi.eastmoney.com/comm/web/getListInfo``
  该接口为东方财富的公开内容列表接口，无需 API 密钥。

市场代码映射：
  - 深圳主板/中小板/创业板（.SZ）→ market_id = 0
  - 上海主板/科创板（.SS）→ market_id = 1
  - 北京证交所（.BJ）→ market_id = 0（暂用深圳市场代码作为回退）

降级处理：任何网络/解析失败均返回占位字符串，调用方无需捕获异常。
"""

from __future__ import annotations

import http.client
import json
import logging
from datetime import datetime
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_API_URL = "https://np-listapi.eastmoney.com/comm/web/getListInfo"
_UA = "tradingagents/0.2 (+https://github.com/TauricResearch/TradingAgents)"

# A股市场代码：.SZ → 0（深圳），.SS → 1（上海），.BJ → 0（北京，回退）
_MARKET_CODE_MAP = {
    "SZ": "0",
    "SS": "1",
    "BJ": "0",
}


def _get_market_id(ticker: str) -> tuple[str, str]:
    """从 ticker 中提取市场代码和纯数字股票代码。

    Returns:
        (market_id, code) 如 ("0", "002979")
    """
    parts = ticker.strip().split(".")
    code = parts[0].strip()
    if len(parts) >= 2:
        suffix = parts[-1].strip().upper()
        market_id = _MARKET_CODE_MAP.get(suffix, "0")
    else:
        # 无交易所后缀，根据代码规则猜测：6开头为上海，其余为深圳
        market_id = "1" if code.startswith("6") else "0"
    return market_id, code


def _in_date_range(date_str: str, start_date: str, end_date: str) -> bool:
    """判断日期字符串是否在 [start_date, end_date] 窗口内。"""
    try:
        article_dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        return start_dt <= article_dt <= end_dt
    except (ValueError, TypeError):
        return True  # 无法解析时默认保留


def get_news_eastmoney(
    ticker: str,
    start_date: str,
    end_date: str,
    limit: int = 20,
    timeout: float = 12.0,
) -> str:
    """抓取东方财富对应 A 股的新闻列表。

    Args:
        ticker: A 股代码，如 ``002979.SZ`` 或 ``600000.SS``。
        start_date: 起始日期（YYYY-MM-DD），用于过滤。
        end_date: 截止日期（YYYY-MM-DD）。
        limit: 最多返回文章数。
        timeout: HTTP 超时秒数。

    Returns:
        格式化的新闻文本块，适合直接注入 LLM Prompt。
    """
    market_id, code = _get_market_id(ticker)
    m_type_and_code = f"{market_id}.{code}"

    params = {
        "client": "web",
        "biz": "web_news_ch",
        "product": "EFDataApi",
        "page_index": 1,
        "page_size": min(limit, 50),  # 接口最大 50
        "req_trace": "web",
        "fields": "Art_Title,Art_ShowTime,Art_Url,Art_Code",
        "mTypeAndCode": m_type_and_code,
        "type": 1,
    }
    url = f"{_API_URL}?{urlencode(params)}"
    req = Request(
        url,
        headers={
            "User-Agent": _UA,
            "Referer": "https://finance.eastmoney.com/",
            "Accept": "application/json, text/plain, */*",
        },
    )

    try:
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except HTTPError as exc:
        logger.warning("东方财富新闻 HTTP 错误 %s: %s", exc.code, ticker)
        return f"<eastmoney news unavailable: HTTP {exc.code}>"
    except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
        logger.warning("东方财富新闻请求失败: %s — %s", ticker, exc)
        return f"<eastmoney news unavailable: {type(exc).__name__}>"

    if not isinstance(data, dict) or data.get("code") != 1:
        msg = data.get("message", "unknown") if isinstance(data, dict) else "invalid response"
        logger.warning("东方财富新闻接口返回错误: %s — %s", ticker, msg)
        return f"<eastmoney news unavailable: API error — {msg}>"

    article_list = (data.get("data") or {}).get("list") or []
    if not article_list:
        return f"东方财富：{start_date} 至 {end_date} 期间未找到 {ticker} 的相关新闻。"

    news_str = ""
    kept = 0
    for article in article_list:
        title = (article.get("Art_Title") or "").strip()
        show_time = (article.get("Art_ShowTime") or "").strip()
        url_str = (article.get("Art_Url") or "").strip()

        if not title:
            continue
        if show_time and not _in_date_range(show_time, start_date, end_date):
            continue

        news_str += f"### {title}\n"
        if show_time:
            news_str += f"发布时间：{show_time}\n"
        if url_str:
            news_str += f"链接：{url_str}\n"
        news_str += "\n"
        kept += 1

    if kept == 0:
        return f"东方财富：{start_date} 至 {end_date} 期间未找到 {ticker} 的相关新闻。"

    return (
        f"## {ticker} 新闻（东方财富，{start_date} 至 {end_date}）：\n\n{news_str}"
    )
