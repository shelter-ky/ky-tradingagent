"""News outages must preserve successful sources without claiming no news."""
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.vendors.yahoo import news


def _article(title, day, ticker="TQQQ"):
    return {"title": title, "relatedTickers": [ticker],
            "providerPublishTime": int(datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp())}


def _offline(*args, **kwargs):
    raise VendorUnavailableError("rate limited")


def test_failed_quote_feed_uses_tagged_search_and_preserves_window(monkeypatch):
    monkeypatch.setattr(news.yf, "Ticker", lambda *a: SimpleNamespace(get_news=_offline))
    monkeypatch.setattr(news.yf, "Search", lambda *a, **k: SimpleNamespace(quotes=[], news=[
        _article("Kept", "2025-05-08"), _article("Future", "2025-05-10"),
        _article("Other symbol", "2025-05-08", "AAPL")]))
    out = news.get_news_yfinance("TQQQ", "2025-05-01", "2025-05-09")
    assert "Kept" in out and "Future" not in out and "Other symbol" not in out


def test_both_quote_and_search_fail_is_unavailable(monkeypatch):
    monkeypatch.setattr(news.yf, "Ticker", lambda *a: SimpleNamespace(get_news=_offline))
    monkeypatch.setattr(news.yf, "Search", _offline)
    with pytest.raises(VendorUnavailableError):
        news.get_news_yfinance("TQQQ", "2025-05-01", "2025-05-09")


@pytest.mark.parametrize("failed_first", [True, False])
def test_global_news_keeps_successes_before_or_after_failed_query(monkeypatch, failed_first):
    monkeypatch.setattr(news, "get_config", lambda: {
        "global_news_queries": ["bad", "good"] if failed_first else ["good", "bad"]})
    def search(query, **kwargs):
        if query == "bad":
            raise VendorUnavailableError("query failed")
        return SimpleNamespace(news=[_article("Kept", "2025-05-08"), _article("Future", "2025-05-10")])
    monkeypatch.setattr(news.yf, "Search", search)
    out = news.get_global_news_yfinance("2025-05-09", 7, 10)
    assert "Kept" in out and "Future" not in out and "Partial coverage" in out


def test_global_all_failed_is_unavailable(monkeypatch):
    monkeypatch.setattr(news, "get_config", lambda: {"global_news_queries": ["a", "b"]})
    monkeypatch.setattr(news.yf, "Search", _offline)
    with pytest.raises(VendorUnavailableError, match="no search query answered"):
        news.get_global_news_yfinance("2025-05-09", 7, 10)


def test_global_partial_empty_does_not_claim_absence(monkeypatch):
    monkeypatch.setattr(news, "get_config", lambda: {"global_news_queries": ["bad", "empty"]})
    def search(query, **kwargs):
        if query == "bad":
            raise VendorUnavailableError("query failed")
        return SimpleNamespace(news=[])
    monkeypatch.setattr(news.yf, "Search", search)
    out = news.get_global_news_yfinance(datetime.now(UTC).strftime("%Y-%m-%d"), 0, 10)
    assert "Partial coverage" in out and "No global news found" not in out
