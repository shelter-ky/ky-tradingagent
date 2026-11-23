# Modified for KY TradingAgent 0.0.1 (2026-11-23).
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.analysts.turn import take_turn
from tradingagents.agents.context import get_instrument_context_from_state, get_language_instruction
from tradingagents.instruments import profile_from_state
from tradingagents.dataflows.macro_events import build_macro_event_context
from tradingagents.agents.tools import (
    get_global_news,
    get_macro_indicators,
    get_news,
    get_prediction_markets,
)

# The tools this analyst is offered; its tool node is built from the same tuple.
TOOLS = (
    get_news,
    get_global_news,
    get_macro_indicators,
    get_prediction_markets,
)


def create_news_analyst(llm):
    def news_analyst_node(state):
        current_date = state["trade_date"]
        asset_type = state.get("asset_type", "stock")
        is_etf = profile_from_state(state).kind == "etf"
        asset_label = "fund and its underlying exposure" if is_etf else "company" if asset_type == "stock" else "asset"
        instrument_context = get_instrument_context_from_state(state)
        # Persist through tool rounds/checkpoint resumes; do not refetch as the
        # model iterates and accidentally change the evidence inside one report.
        macro_context = state.get("macro_event_context") or build_macro_event_context(current_date)

        system_message = (
            f"You are a news researcher tasked with analyzing recent news and trends over the past week. Please write a comprehensive report of the current state of the world that is relevant for trading and macroeconomics. Use the available tools: get_news(start_date, end_date) for news about the {asset_label} under analysis, get_global_news(curr_date, look_back_days, limit) for broader macroeconomic news, get_macro_indicators(indicator, curr_date, look_back_days) to ground macro commentary in actual data from FRED (e.g. 'cpi', 'core_pce', 'unemployment', 'fed_funds_rate', '10y_treasury', 'yield_curve'), and get_prediction_markets(topic, limit) for live market-implied probabilities of forward-looking events (e.g. 'Fed rate cut', 'recession 2026', geopolitical or sector events). Provide specific, actionable insights with supporting evidence to help traders make informed decisions."
            + """ Make sure to append a Markdown table at the end of the report to organize key points in the report, organized and easy to read."""
            + get_language_instruction()
        )
        if is_etf:
            system_message += (
                " For this ETF, separate fund-specific news from index/constituent catalysts."
                " Focus on the next 1–10 trading sessions: verified CPI/PCE, FOMC, Treasury-yield"
                " changes and constituent earnings. Separate consensus, actual releases and revisions;"
                " state unverified dates or missing consensus explicitly rather than inventing them."
            )
        system_message += (
            " The deterministic US macro release evidence below is part of your inputs."
            " Distinguish scheduled from released events, vendor snapshot gaps from"
            " archived pre-release estimates, and prior revisions from actuals."
            " Missing coverage or consensus is a data limitation, never zero or evidence"
            " that no event exists. FRED levels and prediction-market probabilities"
            " cannot replace economic survey consensus. A positive gap is not by itself"
            " bullish or bearish; explain conditional effects on yields and the exposure."
            " Use the named source for each nonmissing expectation, including a public Consensus"
            " supplement. Describe missing consensus per series; when CPI/PPI expectations are"
            " supplied, quote those values instead of labelling all macro consensus unavailable."
            " When news/calendar evidence is labelled archived reference, lead with its original"
            " observation time and the refresh failure. Keep its available dated expectations as"
            " historical evidence, but do not call them confirmed current consensus or infer new"
            " releases, new headlines, no news, or a current surprise from the cached snapshot."
            "\n\n" + macro_context
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Use the provided tools to progress towards answering the question."
                    " If you are unable to fully answer, that's OK; another assistant with different tools"
                    " will help where you left off. Execute what you can to make progress."
                    " Report what your tools support; another agent decides the trade."
                    " You have access to the following tools: {tool_names}."
                    " Today's date is {current_date}; treat it as 'now' for all analysis and tool-call date ranges. {instrument_context}\n"
                    "{system_message}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([tool.name for tool in TOOLS]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        result, report = take_turn(prompt, llm, TOOLS, state["messages"])
        if report:
            report = f"{report}\n\n---\n\n{macro_context}"

        return {
            "messages": [result],
            "news_report": report,
            "macro_event_context": macro_context,
        }

    return news_analyst_node
