"""Index of saved reports: per-instrument tables plus a latest-rating rollup."""

from datetime import datetime

import pytest

from tradingagents.report_summary import parse_final_decision, render_summary_markdown


def _entry(
    instrument,
    run,
    *,
    rating,
    ticker=None,
    price_target=None,
    time_horizon=None,
    entry_price=None,
):
    date, time = run.split("_")
    display = f"{date[:4]}-{date[4:6]}-{date[6:]} {time[:2]}:{time[2:4]}:{time[4:]}"
    return {
        "instrument": instrument,
        "run": run,
        "sort_key": run,
        "time": display,
        "path": f"{instrument}/{run}/complete_report.md",
        "rating": rating,
        "price_target": price_target,
        "time_horizon": time_horizon,
        "entry_price": entry_price,
        "ticker": ticker,
    }


@pytest.mark.unit
def test_render_summary_groups_latest_rating_before_instrument_tables():
    markdown = render_summary_markdown(
        [
            _entry(
                "今世缘",
                "20260804_113000",
                rating="Buy（买入）",
                ticker="603369.SS",
            ),
            _entry(
                "今世缘",
                "20260824_112841",
                rating="Hold（持有）",
                ticker="603369.SS",
                price_target="30.5",
            ),
            _entry(
                "常青科技",
                "20260824_193316",
                rating="Overweight（增持）",
                ticker="603125.SS",
                price_target="30.0",
            ),
            _entry(
                "安孚科技",
                "20260824_193335",
                rating="Sell（卖出）",
                ticker="603031.SS",
            ),
        ],
        generated_at=datetime(2026, 8, 25, 11, 0, 0),
    )

    grouped, remainder = markdown.split("## 今世缘", 1)
    assert grouped.index("## 按最终评级归总") < grouped.index("### Overweight（增持）（1）")
    assert grouped.index("### Overweight（增持）（1）") < grouped.index("### Hold（持有）（1）")
    assert grouped.index("### Hold（持有）（1）") < grouped.index("### Sell（卖出）（1）")
    assert "### Buy（买入）" not in grouped
    assert (
        "- 今世缘（603369.SS），2026-08-24 "
        "[打开](今世缘/20260824_112841/complete_report.md)"
        in grouped.split("### Hold（持有）（1）", 1)[1]
    )
    assert (
        "- 常青科技（603125.SS），2026-08-24 "
        "[打开](常青科技/20260824_193316/complete_report.md)"
        in grouped.split("### Overweight（增持）（1）", 1)[1]
    )
    assert "Buy（买入）" in remainder
    assert "更新时间: 2026-08-25 11:00:00" in grouped


@pytest.mark.unit
def test_rating_rollup_shows_entry_price_only_for_bullish_calls():
    markdown = render_summary_markdown(
        [
            _entry(
                "华勤技术",
                "20260903_200256",
                rating="Overweight（增持）",
                ticker="603296.SS",
                entry_price="78.0",
            ),
            _entry(
                "今世缘",
                "20260804_113000",
                rating="Buy（买入）",
                ticker="603369.SS",
                entry_price="29.0",
            ),
            _entry(
                "英维克",
                "20260902_150805",
                rating="Sell（卖出）",
                ticker="002837.SZ",
                entry_price="33.0",
            ),
            _entry(
                "常青科技",
                "20260824_193316",
                rating="Overweight（增持）",
                ticker="603125.SS",
            ),
        ],
        generated_at=datetime(2026, 9, 4, 10, 0, 0),
    )

    grouped = markdown.split("## 今世缘", 1)[0]
    assert (
        "- 华勤技术（603296.SS），2026-09-03，78.0 "
        "[打开](华勤技术/20260903_200256/complete_report.md)" in grouped
    )
    assert (
        "- 今世缘（603369.SS），2026-08-04，29.0 "
        "[打开](今世缘/20260804_113000/complete_report.md)" in grouped
    )
    assert (
        "- 英维克（002837.SZ），2026-09-02 "
        "[打开](英维克/20260902_150805/complete_report.md)" in grouped
    )
    assert (
        "- 常青科技（603125.SS），2026-08-24 "
        "[打开](常青科技/20260824_193316/complete_report.md)" in grouped
    )


@pytest.mark.unit
def test_parse_final_decision_reads_trader_entry_price():
    report = "\n".join(
        [
            "# Trading Analysis Report: 603296.SS",
            "",
            "### Trader",
            "**Action**: Buy",
            "",
            "**Entry Price**: 78.0",
            "",
            "## V. Portfolio Manager Decision",
            "",
            "**Rating**: Overweight",
            "",
            "**Price Target**: 95.0",
            "",
            "**Time Horizon**: 3-6 months",
        ]
    )

    decision = parse_final_decision(report)

    assert decision["rating"] == "Overweight（增持）"
    assert decision["entry_price"] == "78.0"
    assert decision["price_target"] == "95.0"
