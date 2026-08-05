"""Reusable report-tree writer shared by the CLI and the programmatic API.

Writes a run's per-section markdown (analysts, research, trading, risk,
portfolio) plus consolidated ``complete_report.md`` and ``complete_report.html``
files under ``save_path``. The CLI and ``TradingAgentsGraph.save_reports`` both
call this, so a headless / API run produces the same on-disk report tree a CLI
run does.
"""

import html
import re
from datetime import datetime
from pathlib import Path

from tradingagents.dataflows.symbol_utils import is_cn_a_share, normalize_symbol
from tradingagents.dataflows.utils import safe_ticker_component


_INVALID_REPORT_COMPONENT_RE = re.compile(r"[\x00-\x1f\x7f/\\:]+")
_ORDERED_LIST_RE = re.compile(r"^\s*\d+[.)]\s+(.+)$")
_UNORDERED_LIST_RE = re.compile(r"^\s*[-*+]\s+(.+)$")


def _render_inline_markdown(text: str) -> str:
    rendered = html.escape(text)
    rendered = re.sub(r"`([^`]+)`", r"<code>\1</code>", rendered)
    rendered = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", rendered)
    rendered = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", rendered)
    rendered = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', rendered)
    return rendered


def _is_table_separator(line: str) -> bool:
    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells)


def _split_table_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _render_table(lines: list[str]) -> str:
    header = _split_table_row(lines[0])
    body_rows = [_split_table_row(line) for line in lines[2:]]
    header_html = "".join(f"<th>{_render_inline_markdown(cell)}</th>" for cell in header)
    rows_html = []
    for row in body_rows:
        cells_html = "".join(f"<td>{_render_inline_markdown(cell)}</td>" for cell in row)
        rows_html.append(f"<tr>{cells_html}</tr>")
    return (
        "<table>"
        f"<thead><tr>{header_html}</tr></thead>"
        f"<tbody>{''.join(rows_html)}</tbody>"
        "</table>"
    )


def _markdown_body_to_html(markdown: str) -> str:
    lines = markdown.splitlines()
    blocks: list[str] = []
    paragraph: list[str] = []
    list_items: list[str] = []
    list_tag: str | None = None
    i = 0

    def flush_paragraph() -> None:
        if paragraph:
            text = " ".join(part.strip() for part in paragraph)
            blocks.append(f"<p>{_render_inline_markdown(text)}</p>")
            paragraph.clear()

    def flush_list() -> None:
        nonlocal list_tag
        if list_items and list_tag:
            items = "".join(f"<li>{item}</li>" for item in list_items)
            blocks.append(f"<{list_tag}>{items}</{list_tag}>")
        list_items.clear()
        list_tag = None

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            flush_paragraph()
            flush_list()
            i += 1
            continue

        if stripped.startswith("```"):
            flush_paragraph()
            flush_list()
            language = stripped[3:].strip()
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            language_class = f' class="language-{html.escape(language)}"' if language else ""
            code = html.escape("\n".join(code_lines))
            blocks.append(f"<pre><code{language_class}>{code}</code></pre>")
            i += 1
            continue

        if i + 1 < len(lines) and "|" in line and _is_table_separator(lines[i + 1]):
            flush_paragraph()
            flush_list()
            table_lines = [line, lines[i + 1]]
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                table_lines.append(lines[i])
                i += 1
            blocks.append(_render_table(table_lines))
            continue

        heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading:
            flush_paragraph()
            flush_list()
            level = len(heading.group(1))
            blocks.append(f"<h{level}>{_render_inline_markdown(heading.group(2))}</h{level}>")
            i += 1
            continue

        if re.fullmatch(r"[-*_]{3,}", stripped):
            flush_paragraph()
            flush_list()
            blocks.append("<hr>")
            i += 1
            continue

        unordered = _UNORDERED_LIST_RE.match(line)
        ordered = _ORDERED_LIST_RE.match(line)
        if unordered or ordered:
            flush_paragraph()
            tag = "ul" if unordered else "ol"
            if list_tag and list_tag != tag:
                flush_list()
            list_tag = tag
            item = unordered.group(1) if unordered else ordered.group(1)
            list_items.append(_render_inline_markdown(item))
            i += 1
            continue

        if stripped.startswith(">"):
            flush_paragraph()
            flush_list()
            quote = stripped.lstrip(">").strip()
            blocks.append(f"<blockquote>{_render_inline_markdown(quote)}</blockquote>")
            i += 1
            continue

        flush_list()
        paragraph.append(line)
        i += 1

    flush_paragraph()
    flush_list()
    return "\n".join(blocks)


def _markdown_to_html_document(markdown: str) -> str:
    title_match = re.search(r"^#\s+(.+)$", markdown, flags=re.MULTILINE)
    title = title_match.group(1).strip() if title_match else "Trading Analysis Report"
    body = _markdown_body_to_html(markdown)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    body {{
      color: #1f2933;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.65;
      margin: 0 auto;
      max-width: 980px;
      padding: 32px 24px;
    }}
    h1, h2, h3, h4, h5, h6 {{ color: #102a43; line-height: 1.25; }}
    code, pre {{ background: #f3f4f6; border-radius: 6px; }}
    code {{ padding: 0.15rem 0.3rem; }}
    pre {{ overflow-x: auto; padding: 1rem; }}
    blockquote {{ border-left: 4px solid #bcccdc; color: #52606d; margin-left: 0; padding-left: 1rem; }}
    table {{ border-collapse: collapse; display: block; overflow-x: auto; width: 100%; }}
    th, td {{ border: 1px solid #d9e2ec; padding: 0.5rem 0.75rem; text-align: left; }}
    th {{ background: #f0f4f8; }}
  </style>
</head>
<body>
{body}
</body>
</html>
"""


def resolve_instrument_identity(ticker: str) -> dict:
    """Resolve ticker identity lazily so importing reporting stays lightweight."""
    from tradingagents.agents.utils.agent_utils import resolve_instrument_identity as resolver

    return resolver(ticker)


def resolve_cn_a_share_short_name(ticker: str) -> str | None:
    """Resolve an A-share's Chinese short name lazily via Eastmoney."""
    from tradingagents.dataflows.eastmoney_stock import get_cn_a_share_short_name

    return get_cn_a_share_short_name(ticker)


def _safe_report_component(value: str | None, *, max_len: int = 80) -> str | None:
    """Return a readable, single-directory component or None when unusable."""
    if not isinstance(value, str):
        return None

    cleaned = _INVALID_REPORT_COMPONENT_RE.sub("_", value.strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip(" ._")
    if not cleaned or set(cleaned) == {"."}:
        return None

    return cleaned[:max_len].rstrip(" ._") or None


def report_directory_component(ticker: str) -> str:
    """Prefer the resolved Chinese/company name for report folders, fallback to ticker."""
    normalized_ticker = normalize_symbol(ticker)
    if is_cn_a_share(normalized_ticker):
        cn_short_name = _safe_report_component(resolve_cn_a_share_short_name(normalized_ticker))
        if cn_short_name:
            return cn_short_name
        return safe_ticker_component(normalized_ticker)

    identity = resolve_instrument_identity(normalized_ticker)
    company_name = _safe_report_component(identity.get("company_name"))
    if company_name:
        return company_name
    return safe_ticker_component(normalized_ticker)


def write_report_tree(final_state: dict, ticker: str, save_path) -> Path:
    """Save a completed run's reports to ``save_path``; return the complete-report path."""
    save_path = Path(save_path)
    save_path.mkdir(parents=True, exist_ok=True)
    sections = []

    # 1. Analysts
    analysts_dir = save_path / "1_analysts"
    analyst_parts = []
    if final_state.get("market_report"):
        analysts_dir.mkdir(exist_ok=True)
        (analysts_dir / "market.md").write_text(final_state["market_report"], encoding="utf-8")
        analyst_parts.append(("Market Analyst", final_state["market_report"]))
    if final_state.get("sentiment_report"):
        analysts_dir.mkdir(exist_ok=True)
        (analysts_dir / "sentiment.md").write_text(final_state["sentiment_report"], encoding="utf-8")
        analyst_parts.append(("Sentiment Analyst", final_state["sentiment_report"]))
    if final_state.get("news_report"):
        analysts_dir.mkdir(exist_ok=True)
        (analysts_dir / "news.md").write_text(final_state["news_report"], encoding="utf-8")
        analyst_parts.append(("News Analyst", final_state["news_report"]))
    if final_state.get("fundamentals_report"):
        analysts_dir.mkdir(exist_ok=True)
        (analysts_dir / "fundamentals.md").write_text(final_state["fundamentals_report"], encoding="utf-8")
        analyst_parts.append(("Fundamentals Analyst", final_state["fundamentals_report"]))
    if analyst_parts:
        content = "\n\n".join(f"### {name}\n{text}" for name, text in analyst_parts)
        sections.append(f"## I. Analyst Team Reports\n\n{content}")

    # 2. Research
    if final_state.get("investment_debate_state"):
        research_dir = save_path / "2_research"
        debate = final_state["investment_debate_state"]
        research_parts = []
        if debate.get("bull_history"):
            research_dir.mkdir(exist_ok=True)
            (research_dir / "bull.md").write_text(debate["bull_history"], encoding="utf-8")
            research_parts.append(("Bull Researcher", debate["bull_history"]))
        if debate.get("bear_history"):
            research_dir.mkdir(exist_ok=True)
            (research_dir / "bear.md").write_text(debate["bear_history"], encoding="utf-8")
            research_parts.append(("Bear Researcher", debate["bear_history"]))
        if debate.get("judge_decision"):
            research_dir.mkdir(exist_ok=True)
            (research_dir / "manager.md").write_text(debate["judge_decision"], encoding="utf-8")
            research_parts.append(("Research Manager", debate["judge_decision"]))
        if research_parts:
            content = "\n\n".join(f"### {name}\n{text}" for name, text in research_parts)
            sections.append(f"## II. Research Team Decision\n\n{content}")

    # 3. Trading
    if final_state.get("trader_investment_plan"):
        trading_dir = save_path / "3_trading"
        trading_dir.mkdir(exist_ok=True)
        (trading_dir / "trader.md").write_text(final_state["trader_investment_plan"], encoding="utf-8")
        sections.append(f"## III. Trading Team Plan\n\n### Trader\n{final_state['trader_investment_plan']}")

    # 4. Risk Management
    if final_state.get("risk_debate_state"):
        risk_dir = save_path / "4_risk"
        risk = final_state["risk_debate_state"]
        risk_parts = []
        if risk.get("aggressive_history"):
            risk_dir.mkdir(exist_ok=True)
            (risk_dir / "aggressive.md").write_text(risk["aggressive_history"], encoding="utf-8")
            risk_parts.append(("Aggressive Analyst", risk["aggressive_history"]))
        if risk.get("conservative_history"):
            risk_dir.mkdir(exist_ok=True)
            (risk_dir / "conservative.md").write_text(risk["conservative_history"], encoding="utf-8")
            risk_parts.append(("Conservative Analyst", risk["conservative_history"]))
        if risk.get("neutral_history"):
            risk_dir.mkdir(exist_ok=True)
            (risk_dir / "neutral.md").write_text(risk["neutral_history"], encoding="utf-8")
            risk_parts.append(("Neutral Analyst", risk["neutral_history"]))
        if risk_parts:
            content = "\n\n".join(f"### {name}\n{text}" for name, text in risk_parts)
            sections.append(f"## IV. Risk Management Team Decision\n\n{content}")

        # 5. Portfolio Manager
        if risk.get("judge_decision"):
            portfolio_dir = save_path / "5_portfolio"
            portfolio_dir.mkdir(exist_ok=True)
            (portfolio_dir / "decision.md").write_text(risk["judge_decision"], encoding="utf-8")
            sections.append(f"## V. Portfolio Manager Decision\n\n### Portfolio Manager\n{risk['judge_decision']}")

    # Write consolidated report
    header = f"# Trading Analysis Report: {ticker}\n\nGenerated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    complete_report = header + "\n\n".join(sections)
    (save_path / "complete_report.md").write_text(complete_report, encoding="utf-8")
    (save_path / "complete_report.html").write_text(
        _markdown_to_html_document(complete_report),
        encoding="utf-8",
    )
    return save_path / "complete_report.md"
