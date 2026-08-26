"""Report parity: the shared writer produces the report tree for the CLI and the
programmatic API alike (#1037)."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tradingagents.reporting import report_directory_component, write_report_tree


def _state():
    return {
        "market_report": "MKT",
        "news_report": "NEWS",
        "investment_debate_state": {"judge_decision": "RM PLAN"},
        "trader_investment_plan": "TRADE",
        "risk_debate_state": {"judge_decision": "PM DECISION"},
    }


@pytest.mark.unit
def test_write_report_tree_creates_files(tmp_path):
    out = write_report_tree(_state(), "AAPL", tmp_path)
    assert out.name == "complete_report.md"
    assert (tmp_path / "1_analysts" / "market.md").read_text() == "MKT"
    assert (tmp_path / "1_analysts" / "news.md").read_text() == "NEWS"
    assert (tmp_path / "2_research" / "manager.md").read_text() == "RM PLAN"
    assert (tmp_path / "3_trading" / "trader.md").read_text() == "TRADE"
    assert (tmp_path / "5_portfolio" / "decision.md").read_text() == "PM DECISION"
    complete = out.read_text()
    assert "Trading Analysis Report: AAPL" in complete
    assert "MKT" in complete and "PM DECISION" in complete


@pytest.mark.unit
def test_write_report_tree_creates_html_companion(tmp_path):
    out = write_report_tree(
        {
            "market_report": "Market **strong** signal",
            "news_report": "Use <raw> safely",
        },
        "AAPL",
        tmp_path,
    )

    html = (tmp_path / "complete_report.html").read_text()
    assert out == tmp_path / "complete_report.md"
    assert "<h1>Trading Analysis Report: AAPL</h1>" in html
    assert "<strong>strong</strong>" in html
    assert "&lt;raw&gt;" in html


@pytest.mark.unit
def test_save_reports_explicit_path(tmp_path):
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    # Unbound: with an explicit save_path, the method doesn't touch self/config.
    out = TradingAgentsGraph.save_reports(None, _state(), "AAPL", save_path=tmp_path)
    assert (tmp_path / "complete_report.md").exists()
    assert out == tmp_path / "complete_report.md"


@pytest.mark.unit
def test_save_reports_defaults_under_results_dir(tmp_path):
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    mock_self = SimpleNamespace(config={"results_dir": str(tmp_path)})
    with patch(
        "tradingagents.reporting.lookup_local_ticker_name",
        return_value="工业富联",
    ):
        out = TradingAgentsGraph.save_reports(mock_self, _state(), "601138.SS")
    assert out.exists()
    assert out.parent.parent.parent.name == "reports"  # results_dir/reports/工业富联/<stamp>/...
    assert out.parent.parent.name == "工业富联"
    assert out.parent.name.startswith("20")


@pytest.mark.unit
def test_report_directory_component_prefers_resolved_company_name():
    with patch(
        "tradingagents.reporting.lookup_local_ticker_name",
        return_value=None,
    ), patch(
        "tradingagents.reporting.resolve_instrument_identity",
        return_value={"company_name": "工业富联"},
    ):
        assert report_directory_component("AAPL") == "工业富联"


@pytest.mark.unit
def test_report_directory_component_avoids_english_company_name_for_a_share():
    with patch(
        "tradingagents.reporting.lookup_local_ticker_name",
        return_value=None,
    ), patch(
        "tradingagents.reporting.resolve_cn_a_share_short_name",
        return_value=None,
    ), patch(
        "tradingagents.reporting.resolve_instrument_identity",
        return_value={"company_name": "Shenzhen SDG Information Co., Ltd"},
    ):
        assert report_directory_component("000070.SZ") == "000070.SZ"


@pytest.mark.unit
def test_report_directory_component_prefers_cn_short_name_for_a_share():
    with patch(
        "tradingagents.reporting.lookup_local_ticker_name",
        return_value=None,
    ), patch(
        "tradingagents.reporting.resolve_cn_a_share_short_name",
        return_value="华勤技术",
    ), patch(
        "tradingagents.reporting.resolve_instrument_identity",
        return_value={"company_name": "Huaqin Co., Ltd"},
    ):
        assert report_directory_component("603296.SS") == "华勤技术"


@pytest.mark.unit
def test_report_directory_component_prefers_local_map_over_api():
    with patch(
        "tradingagents.reporting.lookup_local_ticker_name",
        return_value="今世缘",
    ), patch(
        "tradingagents.reporting.resolve_cn_a_share_short_name",
        return_value="接口返回的名字",
    ) as api_lookup:
        assert report_directory_component("603369.SS") == "今世缘"
        api_lookup.assert_not_called()


@pytest.mark.unit
def test_lookup_local_ticker_name_reads_map_file(tmp_path):
    from tradingagents.reporting import load_ticker_name_map, lookup_local_ticker_name

    map_path = tmp_path / "ticker_names.json"
    map_path.write_text('{"601138.SS": "工业富联"}', encoding="utf-8")
    indexed = load_ticker_name_map(map_path)
    assert indexed["601138"] == "工业富联"
    with patch(
        "tradingagents.reporting.load_ticker_name_map",
        return_value=indexed,
    ):
        assert lookup_local_ticker_name("601138.SH") == "工业富联"


@pytest.mark.unit
def test_report_directory_component_sanitizes_resolved_company_name():
    with patch(
        "tradingagents.reporting.lookup_local_ticker_name",
        return_value=None,
    ), patch(
        "tradingagents.reporting.resolve_instrument_identity",
        return_value={"company_name": "工业/富联:AI\n服务器"},
    ):
        assert report_directory_component("AAPL") == "工业_富联_AI_服务器"
