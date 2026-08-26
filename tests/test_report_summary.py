"""Index of saved reports: per-instrument tables plus a latest-rating rollup."""

from datetime import datetime

import pytest

from tradingagents.report_summary import render_summary_markdown


def _entry(
    instrument,
    run,
    *,
    rating,
    ticker=None,
    price_target=None,
    time_horizon=None,
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
    assert "- 今世缘（603369.SS），2026-08-24" in grouped.split("### Hold（持有）（1）", 1)[1]
    assert "- 常青科技（603125.SS），2026-08-24" in grouped.split("### Overweight（增持）（1）", 1)[1]
    assert "Buy（买入）" in remainder
    assert "更新时间: 2026-08-25 11:00:00" in grouped
