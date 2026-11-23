"""Price outages must not turn incomplete or old cache bars into current quotes."""
import os
import pandas as pd
import pytest

from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors.yahoo import market, ohlcv, snapshot
from tradingagents.dataflows.vendors.yahoo.sessions import is_session, latest_completed_session


@pytest.fixture
def prices(monkeypatch, tmp_path):
    monkeypatch.setattr(ohlcv, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    now = pd.Timestamp("2026-11-09 13:00Z")
    monkeypatch.setattr(ohlcv, "_utc_now", lambda: now)
    local_now = pd.Timestamp.fromtimestamp(now.timestamp())
    monkeypatch.setattr(ohlcv.pd.Timestamp, "today", staticmethod(lambda: local_now))
    def offline(*args, **kwargs):
        raise VendorUnavailableError("rate limited")
    monkeypatch.setattr(ohlcv, "yf_retry", offline)
    return tmp_path


def _cache(path, days=("2026-11-05", "2026-11-06"), fetched="2026-11-06 22:00Z", closes=None):
    rows = pd.DataFrame({"Date": days, "Open": 50.0, "High": 51.0, "Low": 49.0,
                         "Close": closes if closes is not None else [50.0] * len(days), "Volume": 1000})
    file = path / "TQQQ-YFin-data.csv"
    rows.to_csv(file, index=False)
    stamp = pd.Timestamp(fetched).timestamp()
    os.utime(file, (stamp, stamp))
    return file


def test_friday_cache_survives_monday_premarket_outage(prices):
    _cache(prices)
    rows = ohlcv.load_ohlcv("TQQQ", "2026-11-09")
    assert rows["Date"].iloc[-1] == pd.Timestamp("2026-11-06")
    assert rows.attrs["market_data"]["refresh_failed"] is True
    assert "not a live quote" in ohlcv.describe_data_source(rows)


@pytest.mark.parametrize("days,fetched", [
    (("2026-11-05",), "2026-11-06 22:00Z"),
    (("2026-11-06",), "2026-11-06 17:00Z"),
    (("2026-11-06",), "2026-11-10 22:00Z"),
])
def test_old_partial_or_future_dated_cache_is_rejected(prices, days, fetched):
    _cache(prices, days, fetched)
    with pytest.raises(VendorUnavailableError, match="no sufficiently complete"):
        ohlcv.load_ohlcv("TQQQ", "2026-11-09")


def test_fallback_discards_partial_and_future_tail(prices):
    _cache(prices, ("2026-11-05", "2026-11-06", "2026-11-09", "2026-11-10"),
           fetched="2026-11-09 12:00Z", closes=[50, 51, 999, 1000])
    rows = ohlcv.load_ohlcv("TQQQ", "2026-11-09", fill_gaps=False)
    assert rows["Close"].tolist() == [50, 51]


def test_price_and_snapshot_report_validated_cache(prices):
    _cache(prices)
    text = market.get_YFin_data_online("TQQQ", "2026-11-05", "2026-11-09")
    assert "validated cache fallback" in text and "not a live quote" in text
    assert "2026-11-06,50.00" in text
    assert "validated cache fallback" in snapshot.build_verified_market_snapshot("TQQQ", "2026-11-09")


def test_repeated_indicators_do_not_repeat_a_failed_refresh(prices, monkeypatch):
    _cache(prices)
    ohlcv.load_ohlcv("TQQQ", "2026-11-09")
    def no_more_requests(*args, **kwargs):
        pytest.fail("one valid fallback should serve repeated indicators during the cooldown")
    monkeypatch.setattr(ohlcv, "yf_retry", no_more_requests)
    assert ohlcv.load_ohlcv("TQQQ", "2026-11-09").attrs["market_data"]["refresh_failed"]


def test_bad_cache_is_replaced_only_by_valid_download(prices, monkeypatch):
    file = prices / "TQQQ-YFin-data.csv"
    file.write_text("wrong,columns\n1,2\n")
    frame = pd.DataFrame({"Date": pd.to_datetime(["2026-11-06"]), "Close": [52.0]}).set_index("Date")
    monkeypatch.setattr(ohlcv, "yf_retry", lambda fn: frame)
    rows = ohlcv.load_ohlcv("TQQQ", "2026-11-09")
    assert rows["Close"].tolist() == [52.0]
    assert "52.0" in file.read_text()


def test_invalid_download_does_not_overwrite_good_cache(prices, monkeypatch):
    file = _cache(prices)
    before = file.read_bytes()
    bad = pd.DataFrame({"Date": pd.to_datetime(["2026-11-06"]), "Close": [float("inf")]}).set_index("Date")
    monkeypatch.setattr(ohlcv, "yf_retry", lambda fn: bad)
    with pytest.raises(NoMarketDataError):
        ohlcv.load_ohlcv("TQQQ", "2026-11-09")
    assert file.read_bytes() == before


def test_non_us_cache_does_not_inherit_us_calendar(prices):
    file = _cache(prices)
    file.rename(prices / "7203.T-YFin-data.csv")
    with pytest.raises(VendorUnavailableError):
        ohlcv.load_ohlcv("7203.T", "2026-11-09")


def test_provisional_live_row_is_labelled(prices, monkeypatch):
    frame = pd.DataFrame({"Date": pd.to_datetime(["2026-11-06", "2026-11-09"]), "Close": [50, 51]}).set_index("Date")
    monkeypatch.setattr(ohlcv, "yf_retry", lambda fn: frame)
    rows = ohlcv.load_ohlcv("TQQQ", "2026-11-09")
    assert "not a final closing price" in ohlcv.describe_data_source(rows)


@pytest.mark.parametrize("as_of,now,expected", [
    ("2026-04-03", "2026-04-03 22:00Z", "2026-04-02"),
    ("2026-06-19", "2026-06-19 22:00Z", "2026-06-18"),
    ("2026-11-27", "2026-11-27 18:35Z", "2026-11-27"),
    ("2026-11-27", "2026-11-27 18:10Z", "2026-11-25"),
    ("2026-12-24", "2026-12-24 18:35Z", "2026-12-24"),
    ("2026-11-09", "2026-11-09 12:00Z", "2026-11-06"),
])
def test_session_boundaries(as_of, now, expected):
    assert latest_completed_session(pd.Timestamp(as_of), pd.Timestamp(now)) == pd.Timestamp(expected)


def test_saturday_new_year_does_not_close_friday():
    assert is_session(pd.Timestamp("2021-12-31"))
