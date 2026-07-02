"""Sentiment analyst — multi-source sentiment analysis for a target ticker.

Previously named ``social_media_analyst``. Renamed and redesigned because
the old version had a prompt that demanded social-media analysis but the
only tool available was Yahoo Finance news — which led LLMs to fabricate
Reddit/X/StockTwits content under prompt pressure (verified live).

The redesigned agent pre-fetches three complementary data sources before
the LLM is invoked and injects them into the prompt as structured blocks.

**Data source routing (automatic, based on ticker suffix)**

For non-A-share tickers (US, HK, etc.):
  1. News headlines     — Yahoo Finance (institutional framing)
  2. StockTwits messages — retail-trader posts indexed by cashtag, with
                           user-labeled Bullish/Bearish sentiment tags
  3. Reddit posts        — r/wallstreetbets, r/stocks, r/investing

For Chinese A-share tickers (.SZ / .SS / .BJ):
  1. News headlines     — 东方财富（Eastmoney）中文财经新闻
  2. 股吧帖子            — 东方财富股吧散户讨论（替代 StockTwits）
  3. 说明文本            — 说明国内无 Reddit 等境外社区，引导 LLM 利用股吧数据

The agent does not use tool-calling; the data is in the prompt from
turn 0. Output uses the structured-output pattern (json_schema for
OpenAI/xAI, response_schema for Gemini, tool-use for Anthropic), falling
back to free-text generation for providers that lack native support, so
the sentiment header (band + score + confidence) is deterministic across
runs and providers instead of free-form per-model prose.

See: https://github.com/TauricResearch/TradingAgents/issues/557
See: https://github.com/TauricResearch/TradingAgents/issues/796
"""

from datetime import datetime, timedelta

from langchain_core.messages import AIMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.schemas import SentimentReport, render_sentiment_report
from tradingagents.agents.utils.agent_utils import (
    get_instrument_context_from_state,
    get_language_instruction,
    get_news,
)
from tradingagents.agents.utils.structured import (
    bind_structured,
    invoke_structured_or_freetext,
)
from tradingagents.dataflows.eastmoney_guba import fetch_guba_posts
from tradingagents.dataflows.eastmoney_news import get_news_eastmoney
from tradingagents.dataflows.reddit import fetch_reddit_posts
from tradingagents.dataflows.stocktwits import fetch_stocktwits_messages
from tradingagents.dataflows.symbol_utils import is_cn_a_share


def _seven_days_back(trade_date: str) -> str:
    return (datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")


def create_sentiment_analyst(llm):
    """Create a sentiment analyst node for the trading graph.

    Pre-fetches news + social/community data, injects them into the
    prompt as structured blocks, and produces a deterministic sentiment
    report via structured output (with a free-text fallback for providers
    that do not support it).

    Automatically routes to Chinese domestic data sources (Eastmoney news +
    Guba stock-bar) for A-share tickers (.SZ / .SS / .BJ), and to
    Yahoo Finance + StockTwits + Reddit for all other markets.
    """
    structured_llm = bind_structured(llm, SentimentReport, "Sentiment Analyst")

    def sentiment_analyst_node(state):
        ticker = state["company_of_interest"]
        end_date = state["trade_date"]
        start_date = _seven_days_back(end_date)
        instrument_context = get_instrument_context_from_state(state)

        if is_cn_a_share(ticker):
            # --- A股数据源：东方财富新闻 + 股吧 ---
            news_block = get_news_eastmoney(ticker, start_date, end_date)
            guba_block = fetch_guba_posts(ticker, limit=30, trade_date=end_date)
            system_message = _build_cn_system_message(
                ticker=ticker,
                start_date=start_date,
                end_date=end_date,
                news_block=news_block,
                guba_block=guba_block,
            )
        else:
            # --- 海外数据源：Yahoo Finance + StockTwits + Reddit ---
            news_block = get_news.func(ticker, start_date, end_date)
            stocktwits_block = fetch_stocktwits_messages(ticker, limit=30)
            reddit_block = fetch_reddit_posts(ticker)
            system_message = _build_system_message(
                ticker=ticker,
                start_date=start_date,
                end_date=end_date,
                news_block=news_block,
                stocktwits_block=stocktwits_block,
                reddit_block=reddit_block,
            )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " If you or any other assistant has the FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** or deliverable,"
                    " prefix your response with FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** so the team knows to stop."
                    " Today's date is {current_date}; treat it as 'now' for all analysis and tool-call date ranges. {instrument_context}"
                    "\n{system_message}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(current_date=end_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        formatted_messages = prompt.format_messages(messages=state["messages"])

        report_text = invoke_structured_or_freetext(
            structured_llm,
            llm,
            formatted_messages,
            render_sentiment_report,
            "Sentiment Analyst",
        )

        return {
            "messages": [AIMessage(content=report_text)],
            "sentiment_report": report_text,
        }

    return sentiment_analyst_node


# ---------------------------------------------------------------------------
# System message builders
# ---------------------------------------------------------------------------

def _build_cn_system_message(
    *,
    ticker: str,
    start_date: str,
    end_date: str,
    news_block: str,
    guba_block: str,
) -> str:
    """为A股构建系统消息，使用东方财富作为数据源。"""
    return f"""You are a financial market sentiment analyst specialising in Chinese A-share markets. Your task is to produce a comprehensive sentiment report for {ticker} covering the period from {start_date} to {end_date}, drawing on two complementary domestic data sources that have already been collected for you.

## 数据来源（已预取，直接在本 Prompt 中）

### ① 财经新闻 — 东方财富（Eastmoney），过去7天
机构面/媒体面信号。信息驱动，变动较慢，可信度较高。

<start_of_news>
{news_block}
<end_of_news>

### ② 东方财富股吧帖子 — 散户社交讨论
东方财富股吧是中国最活跃的A股散户讨论社区，等同于海外的 StockTwits。
帖子的阅读数（阅读量）和回复数（评论数）反映散户关注度和讨论热度。
作者名为"XXX资讯"等官方账号的帖子属于机构/媒体性质，与散户帖子应区分分析。

<start_of_guba>
{guba_block}
<end_of_guba>

### ③ 境外社交平台（Reddit / StockTwits 等）
**说明：** A股（{ticker}）在 Reddit / StockTwits 等境外散户社区没有实质性讨论。
这是 A 股的正常特征，并不代表情绪为空，而是说明该股的散户情绪应完全从国内平台（如股吧）获取。

## 如何分析A股情绪数据（最佳实践）

1. **以股吧帖子数量和阅读/回复比例衡量散户关注度。** 帖子数多、阅读量大说明关注度高；帖子少则说明市场对该股的情绪覆盖稀薄。

2. **区分机构/媒体帖子与散户帖子。** 作者名含"资讯""新闻""研报"等字样的帖子为机构内容；个人用户名的帖子为真正散户声音。

3. **寻找新闻与股吧情绪的背离。** 如果新闻面利好但股吧讨论冷淡（甚至没有散户帖子），说明散户尚未跟进；反之则需要警惕追高风险。

4. **从帖子标题识别主导叙事。** 反复出现的关键词（如"业绩预增""涨停""国产替代"等）即为当前主导情绪主题。

5. **对数据缺口保持诚实。** 若股吧没有帖子，或新闻为空，应明确指出置信度较低，并说明原因。不要凭空捏造情绪信号。

6. **结合催化剂和风险。** 是否出现业绩预告、重大合同、政策利好/利空、高管变动等可能驱动股价的事件。

## 输出字段

Fill the following fields:
- **overall_band**: Exactly one of Bullish / Mildly Bullish / Neutral / Mixed / Mildly Bearish / Bearish. Use Mixed when sources point in clearly different directions; Neutral only when all sources are genuinely silent.
- **overall_score**: A number from 0 (maximally bearish) to 10 (maximally bullish); 5 is neutral. Keep it consistent with overall_band.
- **confidence**: low / medium / high, based on data quality and sample size.
- **narrative**: Full source-by-source breakdown (East Money news, Guba posts), divergences, dominant narrative themes, catalysts and risks, and a markdown summary table of key sentiment signals (direction, source, supporting evidence). Write the narrative in Chinese as this is an A-share analysis.

{get_language_instruction()}"""


def _build_system_message(
    *,
    ticker: str,
    start_date: str,
    end_date: str,
    news_block: str,
    stocktwits_block: str,
    reddit_block: str,
) -> str:
    """Assemble the sentiment-analyst system message with structured data blocks."""
    return f"""You are a financial market sentiment analyst. Your task is to produce a comprehensive sentiment report for {ticker} covering the period from {start_date} to {end_date}, drawing on three complementary data sources that have already been collected for you.

## Data sources (pre-fetched, in this prompt)

### News headlines — Yahoo Finance, past 7 days
Institutional framing. Fact-driven, slower-moving signal.

<start_of_news>
{news_block}
<end_of_news>

### StockTwits messages — retail-trader social platform indexed by cashtag
Fast-moving signal. Each message carries a user-labeled sentiment tag (Bullish / Bearish / no-label) plus the message body.

<start_of_stocktwits>
{stocktwits_block}
<end_of_stocktwits>

### Reddit posts — r/wallstreetbets, r/stocks, r/investing (past 7 days)
Community discussion. Engagement signal via upvote score and comment count. Subreddit character matters (r/wallstreetbets is often contrarian/exuberant; r/stocks more measured; r/investing longer-term).

<start_of_reddit>
{reddit_block}
<end_of_reddit>

## How to analyze this data (best practices)

1. **Read the StockTwits Bullish/Bearish ratio as a leading retail-sentiment signal.** A 70/30 bullish/bearish split is moderately bullish; ≥90/10 may indicate over-extension and contrarian risk; 50/50 is uncertainty. Sample size matters — base rates on the actual message count, not percentages alone.

2. **Look for cross-source divergences.** If news framing is bearish but StockTwits is overwhelmingly bullish, that mismatch is itself a signal — it can mean retail is leaning into a thesis the news flow hasn't caught up to (or vice versa, that retail is chasing while institutions are cautious).

3. **Weight Reddit posts by engagement.** A 400-upvote / 200-comment thread reflects community attention; a 3-upvote post is noise. Read the body excerpts for context — the title alone often misleads.

4. **Distinguish opinion from event.** A news headline ("Nvidia announces $500M Corning deal") is an event; a StockTwits post ("buying NVDA, this is going to moon") is opinion. Both are inputs but should be weighted differently in your conclusions.

5. **Identify recurring narrative themes.** What topic keeps coming up across sources? That's the dominant narrative driving current sentiment.

6. **Be honest about data limits.** If StockTwits returned only a handful of messages, or one or more sources returned an "<unavailable>" placeholder, the sentiment read is less robust — flag this explicitly in the `confidence` field and the narrative. If the sources are silent on a given subreddit, say so.

7. **Identify catalysts and risks** that emerge across sources — news of upcoming earnings, product launches, competitive threats, macro headlines, etc.

8. **Past sentiment is not predictive.** Frame your conclusions as signal for the trader to weigh alongside fundamentals and technicals, not as a price call.

## Output fields

Fill the following fields:

- **overall_band**: Exactly one of Bullish / Mildly Bullish / Neutral / Mixed / Mildly Bearish / Bearish. Use Mixed when sources point in clearly different directions; Neutral only when all sources are genuinely silent.
- **overall_score**: A number from 0 (maximally bearish) to 10 (maximally bullish); 5 is neutral. Keep it consistent with overall_band.
- **confidence**: low / medium / high, based on data quality and sample size.
- **narrative**: Full source-by-source breakdown, divergences, dominant narrative themes, catalysts and risks, and a markdown summary table of key sentiment signals (direction, source, supporting evidence).

{get_language_instruction()}"""


# ---------------------------------------------------------------------------
# Backwards-compatibility shim
# ---------------------------------------------------------------------------
def create_social_media_analyst(llm):
    """Deprecated alias for :func:`create_sentiment_analyst`.

    Kept so existing code that imports ``create_social_media_analyst``
    continues to work.

    .. deprecated::
        Import :func:`create_sentiment_analyst` directly instead.
    """
    import warnings
    warnings.warn(
        "create_social_media_analyst is deprecated and will be removed in a "
        "future version. Use create_sentiment_analyst instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return create_sentiment_analyst(llm)
