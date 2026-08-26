"""Roll every saved report's final call into one ``reports/汇总.md`` index.

Pure text processing: the summary is rebuilt from what is already on disk, so
it never needs an LLM and stays correct even when reports are added, removed,
or edited by hand.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

SUMMARY_FILENAME = "汇总.md"
_COMPLETE_REPORT_NAME = "complete_report.md"
_PM_SECTION_RE = re.compile(r"^##\s+V\.\s+Portfolio Manager Decision\s*$", re.MULTILINE)
_TICKER_RE = re.compile(r"^#\s+Trading Analysis Report:\s*(.+)$", re.MULTILINE)
_RUN_STAMP_RE = re.compile(r"^(\d{8})_(\d{6})$")
_PLACEHOLDER = "—"

# Canonical ratings, ordered from most bullish to most bearish.
_RATINGS = {
    "buy": ("Buy", "买入"),
    "overweight": ("Overweight", "增持"),
    "hold": ("Hold", "持有"),
    "underweight": ("Underweight", "减持"),
    "sell": ("Sell", "卖出"),
}
_CN_RATINGS = {cn: key for key, (_, cn) in _RATINGS.items()}

# Structured runs render ``**Rating**: Overweight``; older free-text runs wrote
# the call as a Chinese heading or bolded line instead.
_RATING_PATTERNS = (
    re.compile(r"^\*\*Rating\*\*[:：]\s*\**\s*([A-Za-z ]+)", re.MULTILINE),
    re.compile(
        r"^[#\-*\s]*\**\s*(?:最终)?(?:投资)?(?:评级|决策)\**\s*[:：]\s*\**\s*"
        r"([A-Za-z]+|[\u4e00-\u9fff]{2})",
        re.MULTILINE,
    ),
    re.compile(r"FINAL TRANSACTION PROPOSAL[:：]\s*\**\s*([A-Za-z]+)"),
)
_PRICE_TARGET_RE = re.compile(r"^\*\*Price Target\*\*[:：]\s*(.+)$", re.MULTILINE)
_TIME_HORIZON_RE = re.compile(r"^\*\*Time Horizon\*\*[:：]\s*(.+)$", re.MULTILINE)


def _normalize_rating(raw: str) -> str | None:
    token = raw.strip().strip("*").strip()
    key = _CN_RATINGS.get(token) or token.lower()
    match = _RATINGS.get(key)
    if not match:
        return None
    english, chinese = match
    return f"{english}（{chinese}）"


def _portfolio_section(report_md: str) -> str:
    match = _PM_SECTION_RE.search(report_md)
    return report_md[match.end() :] if match else report_md


def parse_final_decision(report_md: str) -> dict:
    """Extract the Portfolio Manager's final call from a complete report."""
    section = _portfolio_section(report_md)
    rating = None
    for pattern in _RATING_PATTERNS:
        for candidate in pattern.finditer(section):
            rating = _normalize_rating(candidate.group(1))
            if rating:
                break
        if rating:
            break

    price_target = _PRICE_TARGET_RE.search(section)
    time_horizon = _TIME_HORIZON_RE.search(section)
    ticker = _TICKER_RE.search(report_md)
    return {
        "rating": rating,
        "price_target": price_target.group(1).strip() if price_target else None,
        "time_horizon": time_horizon.group(1).strip() if time_horizon else None,
        "ticker": ticker.group(1).strip() if ticker else None,
    }


def _run_timestamp(run_dir_name: str) -> tuple[str, str]:
    """Return (sort key, display time) for a ``YYYYMMDD_HHMMSS`` run directory."""
    match = _RUN_STAMP_RE.match(run_dir_name)
    if not match:
        return run_dir_name, run_dir_name
    date, time = match.groups()
    display = (
        f"{date[:4]}-{date[4:6]}-{date[6:]} {time[:2]}:{time[2:4]}:{time[4:]}"
    )
    return run_dir_name, display


def collect_report_entries(reports_root) -> list[dict]:
    """Scan ``reports_root`` for saved runs, newest last within each instrument."""
    reports_root = Path(reports_root)
    if not reports_root.is_dir():
        return []

    entries = []
    for instrument_dir in sorted(p for p in reports_root.iterdir() if p.is_dir()):
        for run_dir in sorted(p for p in instrument_dir.iterdir() if p.is_dir()):
            report_file = run_dir / _COMPLETE_REPORT_NAME
            if not report_file.is_file():
                continue
            try:
                report_md = report_file.read_text(encoding="utf-8")
            except OSError:
                continue
            sort_key, display_time = _run_timestamp(run_dir.name)
            entries.append(
                {
                    "instrument": instrument_dir.name,
                    "run": run_dir.name,
                    "sort_key": sort_key,
                    "time": display_time,
                    "path": report_file.relative_to(reports_root).as_posix(),
                    **parse_final_decision(report_md),
                }
            )
    return entries


def _escape_cell(value: str | None) -> str:
    if not value:
        return _PLACEHOLDER
    return " ".join(value.split()).replace("|", r"\|")


def _latest_run(runs: list[dict]) -> dict:
    return max(runs, key=lambda item: item["sort_key"])


def _instrument_label(instrument: str, run: dict) -> str:
    ticker = run.get("ticker")
    name = f"{instrument}（{ticker}）" if ticker else instrument
    report_date = (run.get("time") or "").split(" ", 1)[0]
    return f"{name}，{report_date}" if report_date else name


def _rating_sort_key(rating: str | None) -> tuple[int, str]:
    if not rating:
        return (len(_RATINGS), "")
    for index, (english, chinese) in enumerate(_RATINGS.values()):
        if rating == f"{english}（{chinese}）":
            return (index, rating)
    return (len(_RATINGS), rating)


def _render_rating_rollup(instruments: dict[str, list[dict]]) -> list[str]:
    """Group each instrument by the rating of its newest saved report."""
    buckets: dict[str, list[str]] = {}
    for instrument in sorted(instruments):
        latest = _latest_run(instruments[instrument])
        rating = latest.get("rating") or "未解析"
        buckets.setdefault(rating, []).append(_instrument_label(instrument, latest))

    lines = [
        "",
        "## 按最终评级归总",
        "",
        "各标的取最新一份报告的评级。",
    ]
    for rating in sorted(buckets, key=_rating_sort_key):
        names = buckets[rating]
        lines.extend(["", f"### {rating}（{len(names)}）", ""])
        lines.extend(f"- {name}" for name in names)
    return lines


def render_summary_markdown(entries: list[dict], *, generated_at=None) -> str:
    """Render the per-instrument summary document."""
    generated_at = generated_at or datetime.now()
    instruments: dict[str, list[dict]] = {}
    for entry in entries:
        instruments.setdefault(entry["instrument"], []).append(entry)

    lines = [
        "# 报告汇总",
        "",
        f"更新时间: {generated_at.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"标的数: {len(instruments)} ｜ 报告数: {len(entries)}",
    ]
    if instruments:
        lines.extend(_render_rating_rollup(instruments))

    for instrument in sorted(instruments):
        runs = sorted(instruments[instrument], key=lambda item: item["sort_key"])
        tickers = {run["ticker"] for run in runs if run["ticker"]}
        title = f"{instrument}（{'、'.join(sorted(tickers))}）" if tickers else instrument
        lines.extend(
            [
                "",
                f"## {title}",
                "",
                "| 报告时间 | Rating | 目标价 | 持有周期 | 报告 |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for run in runs:
            lines.append(
                "| {time} | {rating} | {price_target} | {time_horizon} | [打开]({path}) |".format(
                    time=run["time"],
                    rating=_escape_cell(run["rating"]),
                    price_target=_escape_cell(run["price_target"]),
                    time_horizon=_escape_cell(run["time_horizon"]),
                    path=run["path"],
                )
            )

    return "\n".join(lines) + "\n"


def update_reports_summary(reports_root) -> Path:
    """Rebuild ``<reports_root>/汇总.md`` from every report on disk."""
    reports_root = Path(reports_root)
    summary_path = reports_root / SUMMARY_FILENAME
    reports_root.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        render_summary_markdown(collect_report_entries(reports_root)),
        encoding="utf-8",
    )
    return summary_path
