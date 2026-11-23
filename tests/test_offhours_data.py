"""Closures, outages, capture-time boundaries and missing-news evidence."""
import json
import os
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from tradingagents.dataflows import macro_events as macro
from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.vendors.yahoo import news_cache, ohlcv, snapshot


def offline(*a, **kw):
    raise VendorUnavailableError("rate limited")


def write_prices(path, days, written):
    f = path / 'TQQQ-YFin-data.csv'
    pd.DataFrame({'Date': days, 'Open': 50, 'High': 51, 'Low': 49,
                  'Close': [50 + i for i in range(len(days))], 'Volume': 1000}).to_csv(f, index=False)
    stamp = pd.Timestamp(written).timestamp()
    os.utime(f, (stamp, stamp))
    return f


def price_clock(monkeypatch, now):
    now = pd.Timestamp(now)
    monkeypatch.setattr(ohlcv, '_utc_now', lambda: now)
    monkeypatch.setattr(ohlcv.pd.Timestamp, 'today', staticmethod(lambda: pd.Timestamp.fromtimestamp(now.timestamp())))
    monkeypatch.setattr(ohlcv, 'yf_retry', offline)


@pytest.mark.parametrize('now,asof,days,written', [
    ('2026-11-09 13:00Z', '2026-11-09', ['2026-11-05', '2026-11-06'], '2026-11-06 22:00Z'),
    ('2026-11-08 13:00Z', '2026-11-08', ['2026-11-05', '2026-11-06'], '2026-11-06 22:00Z'),
    ('2026-11-10 04:00Z', '2026-11-10', ['2026-11-06', '2026-11-09'], '2026-11-09 22:00Z'),
    ('2026-06-19 16:00Z', '2026-06-19', ['2026-06-17', '2026-06-18'], '2026-06-18 22:00Z'),
])
def test_closed_exchange_reuses_confirmed_cache_without_network(tmp_path, monkeypatch, now, asof, days, written):
    price_clock(monkeypatch, now)
    write_prices(tmp_path, days, written)
    monkeypatch.setattr(ohlcv, 'yf_retry', lambda *a: pytest.fail('closure must not need a refresh'))
    with run_config({'data_cache_dir': str(tmp_path)}):
        rows = ohlcv.load_ohlcv('TQQQ', asof)
    meta = rows.attrs['market_data']
    assert rows['Date'].iloc[-1] == pd.Timestamp(days[-1])
    assert not meta['missing_completed_session'] and not meta['historical_reference']
    assert 'market closed' in meta['source']


def test_intraday_saved_tail_never_becomes_a_final_close_by_waiting(tmp_path, monkeypatch):
    price_clock(monkeypatch, '2026-11-10 04:00Z')
    f = write_prices(tmp_path, ['2026-11-05', '2026-11-06', '2026-11-09'], '2026-11-09 15:12Z')
    before = f.read_bytes()
    with run_config({'data_cache_dir': str(tmp_path)}):
        rows = ohlcv.load_ohlcv('TQQQ', '2026-11-10', fill_gaps=False)
        text = snapshot.build_verified_market_snapshot('TQQQ', '2026-11-10')
    assert rows['Date'].iloc[-1] == pd.Timestamp('2026-11-06')
    assert rows.attrs['market_data']['missing_completed_session']
    assert rows.attrs['market_data']['historical_reference']
    assert 'HISTORICAL REFERENCE ONLY' in text
    assert '2026-11-09' in text and '| Close | 51.00 |' in text
    assert f.read_bytes() == before


@pytest.mark.parametrize('days,written', [
    (['2026-10-20'], '2026-10-21 22:00Z'),
    (['2026-11-06'], '2026-11-11 22:00Z'),
    (['2026-11-09'], '2026-11-09 15:12Z'),
])
def test_reference_rejects_too_old_future_or_only_partial_cache(tmp_path, monkeypatch, days, written):
    price_clock(monkeypatch, '2026-11-10 04:00Z')
    write_prices(tmp_path, days, written)
    with run_config({'data_cache_dir': str(tmp_path)}), pytest.raises(VendorUnavailableError):
        ohlcv.load_ohlcv('TQQQ', '2026-11-10')


def test_reference_does_not_satisfy_an_uncovered_requested_start(tmp_path, monkeypatch):
    price_clock(monkeypatch, '2026-11-10 04:00Z')
    write_prices(tmp_path, ['2026-11-06', '2026-11-09'], '2026-11-09 15:12Z')
    with run_config({'data_cache_dir': str(tmp_path)}), pytest.raises(VendorUnavailableError):
        ohlcv.load_ohlcv('TQQQ', '2026-11-10', start_date='2026-10-01')


def test_during_regular_session_complete_old_cache_still_refreshes(tmp_path, monkeypatch):
    price_clock(monkeypatch, '2026-11-10 16:00Z')
    write_prices(tmp_path, ['2026-11-06', '2026-11-09'], '2026-11-09 22:00Z')
    calls = []
    def refresh(fn):
        calls.append(1)
        raise VendorUnavailableError('offline')
    monkeypatch.setattr(ohlcv, 'yf_retry', refresh)
    with run_config({'data_cache_dir': str(tmp_path)}):
        ohlcv.load_ohlcv('TQQQ', '2026-11-10')
    assert calls == [1]


@pytest.mark.parametrize('age_hours', [1, 23, 25])
def test_macro_outage_preserves_only_recent_original_captures(tmp_path, monkeypatch, age_hours):
    now = datetime(2026, 11, 10, 15, tzinfo=UTC)
    observed = now - timedelta(hours=age_hours)
    warnings = []
    e = macro._event('CPI MM, SA', '2026-11-10T13:30:00Z', 'Oct', 'yahoo',
                     actual=None, expected=.3, previous=.4, unit='%')
    macro._store(tmp_path / macro.VERSION, [e], observed, warnings)
    monkeypatch.setattr(macro, '_now', lambda: now)
    monkeypatch.setattr(macro, 'get_current_date', lambda: '2026-11-10')
    monkeypatch.setattr(macro, '_fetch', offline)
    before = {f.name: f.read_bytes() for f in (tmp_path / macro.VERSION).glob('*.json')}
    with run_config({'data_cache_dir': str(tmp_path)}):
        text = macro.build_macro_event_context('2026-11-10')
    if age_hours <= 24:
        assert 'ARCHIVED CALENDAR REFERENCE ONLY' in text
        assert observed.isoformat() in text and '0.3 %' in text
        if observed < datetime(2026, 11, 10, 13, 30, tzinfo=UTC):
            assert 'archive predates release; new actual/gap unavailable' in text
    else:
        assert 'Showing 0 focused' in text
    assert before == {f.name: f.read_bytes() for f in (tmp_path / macro.VERSION).glob('*.json')}


def test_macro_fallback_never_promotes_previous_actual_or_future_capture(tmp_path, monkeypatch):
    now = datetime(2026, 11, 10, 15, tzinfo=UTC)
    e = macro._event('CPI MM, SA', '2026-11-10T13:30:00Z', 'Oct', 'yahoo', actual=9.99, expected=.3, unit='%')
    macro._store(tmp_path / macro.VERSION, [e], now - timedelta(hours=1), [])
    macro._store(tmp_path / macro.VERSION, [dict(e, expected=macro.quantity('8%'))], now + timedelta(hours=1), [])
    monkeypatch.setattr(macro, '_now', lambda: now)
    monkeypatch.setattr(macro, 'get_current_date', lambda: '2026-11-10')
    monkeypatch.setattr(macro, '_fetch', offline)
    with run_config({'data_cache_dir': str(tmp_path)}):
        text = macro.build_macro_event_context('2026-11-10')
    assert '9.99' not in text and '8 %' not in text
    assert '0.3 %' in text and '| missing | 0.3 % |' in text


def news_tool(tmp_path, monkeypatch):
    clock = [datetime(2026, 11, 10, 4, tzinfo=UTC)]
    monkeypatch.setattr(news_cache, '_now', lambda: clock[0])
    calls = []
    fail = [False]
    @news_cache.cached_news
    def get_news(ticker, start_date, end_date):
        calls.append(ticker)
        if fail[0]:
            raise VendorUnavailableError('offline')
        return f'## {ticker} News\n\n### A dated article\nPublished: 2026-11-10T03:00:00Z\n'
    return clock, calls, fail, get_news


def test_news_short_cache_then_outage_keeps_original_time(tmp_path, monkeypatch):
    clock, calls, fail, tool = news_tool(tmp_path, monkeypatch)
    with run_config({'data_cache_dir': str(tmp_path)}):
        first = tool('TQQQ', '2026-11-03', '2026-11-10')
        fail[0] = True
        recent = tool('TQQQ', '2026-11-03', '2026-11-10')
        assert len(calls) == 1 and 'local cache' in recent
        clock[0] += timedelta(minutes=6)
        stale = tool('TQQQ', '2026-11-03', '2026-11-10')
    assert len(calls) == 2 and 'ARCHIVED NEWS REFERENCE ONLY' in stale
    assert '2026-11-10T04:00:00+00:00' in stale
    assert 'live response' in first


@pytest.mark.parametrize('change', ['ticker', 'window', 'future_capture', 'corrupt', 'historical'])
def test_news_wrong_identity_future_corrupt_or_historical_not_reused(tmp_path, monkeypatch, change):
    clock, calls, fail, tool = news_tool(tmp_path, monkeypatch)
    with run_config({'data_cache_dir': str(tmp_path)}):
        tool('TQQQ', '2026-11-03', '2026-11-10')
        f = next((tmp_path / news_cache.VERSION).glob('*.json'))
        if change == 'future_capture':
            data = json.loads(f.read_text()); data['observed_at'] = (clock[0]+timedelta(hours=1)).isoformat()
            f.write_text(json.dumps(data), encoding='utf-8')
        if change == 'corrupt':
            f.write_text('{bad', encoding='utf-8')
        if change == 'historical':
            clock[0] += timedelta(days=1)
        fail[0] = True
        with pytest.raises(VendorUnavailableError):
            tool('QQQ' if change == 'ticker' else 'TQQQ',
                 '2026-11-04' if change == 'window' else '2026-11-03', '2026-11-10')
