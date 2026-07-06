"""东方财富股吧（Guba）帖子抓取器 — A股情绪数据

替代 StockTwits，专门为 A 股（.SZ/.SS/.BJ）提供股吧情绪信号。
东方财富股吧是中国最活跃的股票散户讨论社区，帖子携带阅读数和回复数，
可作为散户关注度与讨论热度的代理指标。

数据来源：``guba.eastmoney.com/list,{code}.html``（普通讨论列表）和
``guba.eastmoney.com/list,{code},1,f.html``（资讯/媒体流），均为公开页面，无需 API 密钥。
解析股票代码从 ticker 中提取（002979.SZ → 002979），然后抓取当天发布的帖子。

降级处理：任何网络/解析失败均返回占位字符串，调用方无需捕获异常。
"""

from __future__ import annotations

import html
import http.client
import logging
import re
import time
from datetime import datetime, timedelta
from urllib.error import HTTPError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_DISCUSSION_URL = "https://guba.eastmoney.com/list,{code}.html"
_MEDIA_URL = "https://guba.eastmoney.com/list,{code},1,f.html"
_UA = "tradingagents/0.2 (+https://github.com/TauricResearch/TradingAgents)"

# 股吧每行帖子的正则模式
_RE_ROW = re.compile(r'<tr class="listitem">(.*?)</tr>', re.DOTALL)
_RE_READ = re.compile(r'class="read">(\d+)')
_RE_REPLY = re.compile(r'class="reply">(\d+)')
_RE_TITLE = re.compile(r'data-postid="\d+"[^>]*>([^<]+)</a>')
_RE_AUTHOR = re.compile(r'class="author">.*?<a[^>]*>([^<]+)</a>', re.DOTALL)
_RE_UPDATE = re.compile(r'class="update">([^<]+)</div>')


def _extract_code(ticker: str) -> str:
    """从 ticker 中提取纯数字代码，例如 '002979.SZ' → '002979'。"""
    return ticker.split(".")[0].strip().upper()


def _parse_post_time(time_str: str, trade_date: str) -> datetime | None:
    """将股吧时间字符串（如 '07-01 09:15' 或 '2026-06-30 10:12'）解析为 datetime。"""
    time_str = time_str.strip()
    if not time_str:
        return None
    try:
        # 如果包含年份，直接解析
        if len(time_str) >= 10 and time_str[4] == "-":
            return datetime.strptime(time_str[:16], "%Y-%m-%d %H:%M")
        # 格式: MM-DD HH:MM（无年份，假设当年）
        year = datetime.strptime(trade_date, "%Y-%m-%d").year if trade_date else datetime.now().year
        return datetime.strptime(f"{year}-{time_str}", "%Y-%m-%d %H:%M")
    except (ValueError, AttributeError):
        return None


def _fetch_guba_html(url: str, ticker: str, timeout: float) -> str | None:
    req = Request(
        url,
        headers={
            "User-Agent": _UA,
            "Referer": "https://guba.eastmoney.com/",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        logger.warning("东方财富股吧 HTTP 错误 %s: %s — %s", exc.code, ticker, exc)
    except (OSError, http.client.HTTPException) as exc:
        logger.warning("东方财富股吧请求失败: %s — %s", ticker, exc)
    return None


def _parse_posts(
    raw: str,
    limit: int,
    cutoff: datetime,
    trade_date: str,
) -> list[dict[str, object]]:
    rows = _RE_ROW.findall(raw)
    posts = []
    for row in rows:
        title_m = _RE_TITLE.search(row)
        if not title_m:
            continue
        title = html.unescape(title_m.group(1).strip())
        if not title:
            continue

        read_m = _RE_READ.search(row)
        reply_m = _RE_REPLY.search(row)
        author_m = _RE_AUTHOR.search(row)
        update_m = _RE_UPDATE.search(row)

        read_count = int(read_m.group(1)) if read_m else 0
        reply_count = int(reply_m.group(1)) if reply_m else 0
        author = html.unescape(author_m.group(1).strip()) if author_m else "?"
        update_str = update_m.group(1).strip() if update_m else ""

        # 时间过滤：只保留 lookback_days 内的帖子
        post_dt = _parse_post_time(update_str, trade_date)
        if post_dt and post_dt < cutoff:
            continue

        posts.append({
            "title": title,
            "author": author,
            "read": read_count,
            "reply": reply_count,
            "time": update_str,
        })
        if len(posts) >= limit:
            break
    return posts


def _dedupe_posts(posts: list[dict[str, object]]) -> list[dict[str, object]]:
    seen = set()
    deduped = []
    for p in posts:
        key = (str(p["author"]), str(p["title"]))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(p)
    return deduped


def fetch_guba_posts(
    ticker: str,
    limit: int = 20,
    lookback_days: int = 7,
    timeout: float = 12.0,
    trade_date: str | None = None,
) -> str:
    """抓取东方财富股吧近期帖子并返回格式化文本，适合直接注入 LLM Prompt。

    Args:
        ticker: 股票代码，如 ``002979.SZ`` 或 ``600000.SS``。
        limit: 每个来源最多抓取的帖子数；两个来源合并后会去重输出。
        lookback_days: 只保留最近 N 天内的帖子。
        timeout: HTTP 请求超时秒数。
        trade_date: 分析日期（YYYY-MM-DD），用于解析帖子相对时间，默认今天。

    Returns:
        格式化的帖子列表字符串，或占位符说明。
    """
    code = _extract_code(ticker)
    if not code:
        return f"<guba unavailable: cannot extract stock code from {ticker!r}>"

    trade_date = trade_date or datetime.now().strftime("%Y-%m-%d")
    cutoff = datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=lookback_days)

    discussion_raw = _fetch_guba_html(_DISCUSSION_URL.format(code=code), ticker, timeout)
    media_raw = _fetch_guba_html(_MEDIA_URL.format(code=code), ticker, timeout)
    if not discussion_raw and not media_raw:
        return f"<guba unavailable: failed to fetch discussion and media streams for {ticker}>"

    if discussion_raw and not _RE_ROW.findall(discussion_raw) and not media_raw:
        return f"<东方财富股吧：{ticker} 无帖子（HTML 结构可能已变更）>"

    discussion_posts = _parse_posts(discussion_raw, limit, cutoff, trade_date) if discussion_raw else []
    media_posts = _parse_posts(media_raw, limit, cutoff, trade_date) if media_raw else []
    all_posts = _dedupe_posts(discussion_posts + media_posts)

    if not all_posts:
        return (
            f"<东方财富股吧：{ticker}（代码 {code}）过去 {lookback_days} 天内未找到帖子。"
            "这可能表示近期该股在股吧缺乏散户讨论热度。>"
        )

    total_reads = sum(int(p["read"]) for p in all_posts)
    total_replies = sum(int(p["reply"]) for p in all_posts)

    lines = [
        f"东方财富股吧 — {ticker}（代码 {code}）",
        f"过去 {lookback_days} 天内从两个公开股吧列表合并找到 {len(all_posts)} 个帖子 "
        f"· 总阅读 {total_reads:,} · 总回复 {total_replies:,}",
        "",
    ]
    for p in all_posts:
        meta = f"{p['time']} · 阅读 {p['read']:,} · 回复 {p['reply']}"
        lines.append(f"  [{meta}] {p['author']}: {p['title']}")

    return "\n".join(lines).rstrip()
