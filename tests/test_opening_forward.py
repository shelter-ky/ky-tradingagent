"""Offline tests using generated prices and fictional account fixtures."""
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pytest

from tradingagents.dataflows import intraday as data
from tradingagents.forward_book import ForwardBook
from tradingagents.opening import OpeningEngine, vxn_inputs
from tradingagents.opening_rule import STRATEGY, opening_target
from tradingagents.portfolio import PortfolioContext
from tests.test_intraday import bars


START = pd.Timestamp("2026-11-12 09:35:12", tz="America/New_York")


def setup(tmp, at=START, previous=18):
    book = ForwardBook(tmp / "book.sqlite3")
    clock = [at]

    def minutes(ticker):
        return bars(n=6, slope=0, date=str(clock[0].date()))

    engine = OpeningEngine(
        "TQQQ",
        book,
        clock=lambda: clock[0],
        bars=minutes,
        vxn=lambda day: (previous, 20, "2026-11-11"),
        premarket=lambda day: {
            "date": day,
            "premarket": {"status": "AVAILABLE", "direction": "WEAK"},
        },
    )
    return book, engine, clock


@pytest.mark.parametrize(
    ("vxn", "current", "gate", "expected"),
    [
        (17.99, 0, True, 1),
        (18, 1, True, 0.5),
        (20, 1, True, 0.5),
        (20.01, 0.5, True, 0),
        (20.01, 0.5, False, 0.5),
        (18, 1, False, 1),
        (18, 0, False, 0.5),
    ],
)
def test_shared_rule(vxn, current, gate, expected):
    assert opening_target(current, vxn, 20, gate) == expected


def test_vxn_uses_exact_21_prior_sessions():
    from tradingagents.dataflows.vendors.yahoo.sessions import is_session

    day = pd.Timestamp("2026-11-12")
    days = [d for d in pd.date_range("2026-09-01", day) if is_session(d)][-22:]
    frame = pd.DataFrame(
        {"DATE": [d.strftime("%m/%d/%Y") for d in days], "CLOSE": [10] * 20 + [18, 999]}
    )
    assert vxn_inputs(frame, "2026-11-12") == (18, 10, "2026-11-11")
    with pytest.raises(ValueError):
        vxn_inputs(frame.drop(index=10), "2026-11-12")


def test_technical_recomputes_both_snapshots_at_shared_minute(monkeypatch):
    feed = {"TQQQ": bars(n=35), "QQQ": bars(n=34)}
    feed["TQQQ"].iloc[-1] = [999, 1000, 998, 999, 200]
    monkeypatch.setattr(data, "fetch_regular_bars", lambda ticker: feed[ticker])
    a, b = data.paired_snapshots("TQQQ", "QQQ", lambda: pd.Timestamp("2026-11-12T15:05:12Z"))
    assert a.bar_end == b.bar_end and a.price == b.price and a.ema20 == b.ema20


def test_before_after_window_and_frozen_across_restart(tmp_path):
    book, e, clock = setup(tmp_path)
    clock[0] = START - pd.Timedelta(minutes=1)
    assert e.step()["action"] == "WAIT"
    clock[0] = START
    r = e.step()
    assert (r["status"], r["action"], r["opening"]["target"]) == ("READY", "WATCH_RISK", 0.5)
    assert len(r["snapshots"]) == 2
    other = setup(tmp_path, previous=9)[1]
    assert other.step()["opening"] == r["opening"]
    clock[0] = START + pd.Timedelta(minutes=1)
    assert e.step()["action"] == "WAIT"
    clock[0] += pd.Timedelta(days=1)
    assert e.step()["status"] == "WAITING" and "opening" not in e.step()
    clock[0] -= pd.Timedelta(minutes=1)
    assert e.step()["opening"]["previous"] == 0.5


def test_gap_missing_premarket_and_late_data(tmp_path):
    _, e, clock = setup(tmp_path)
    e.bars = lambda t: bars(n=5).drop(bars(n=5).index[2])
    assert e.step()["status"] == "UNAVAILABLE"

    def feed(t):
        clock[0] += pd.Timedelta(seconds=26)
        return bars(n=5, slope=0)

    e.bars = feed
    assert e.step()["action"] == "WAIT"
    clock[0] = START
    e.bars = lambda t: bars(n=5, slope=0)
    e.context.clear()
    e.premarket = lambda day: {"date": day, "premarket": {"status": "UNAVAILABLE"}}
    assert e.step()["opening"]["pre"] == "MISSING"


def book_snapshot(available=65, minutes=0):
    return PortfolioContext.model_validate(
        dict(
            currency="USD",
            cash=500,
            net_asset_value=7000,
            positions=[dict(ticker="TQQQ", quantity=65, available_quantity=available)],
            intraday_stop_prices={"TQQQ": 90},
            as_of_time=(START - pd.Timedelta(minutes=minutes)).isoformat(),
        )
    )


def test_sizing_requires_sellable_synchronized_snapshot_and_limits(tmp_path):
    _, e, _ = setup(tmp_path)
    limits = dict(
        max_position_weight=0.8,
        max_daily_exposure_weight=2,
        max_stop_scenario_loss_fraction=0.2,
        min_cash_weight=0.05,
        slippage_bps=0,
        fee_per_order=5,
    )
    assert e.step()["sizing"]["status"] == "NEEDS_INPUT"
    size = e.step(portfolio=book_snapshot(), limits=limits)["sizing"]
    assert size["validated_allocation"]["delta_quantity"] == -30
    assert size["validated_allocation"]["target_quantity"] == 35
    for book in (book_snapshot(available=29), book_snapshot(minutes=4)):
        assert e.step(portfolio=book, limits=limits)["sizing"]["validated_allocation"] is None


def test_date_only_checkpoint_identity_is_stable():
    import hashlib

    old_json = '{"cash":100.0,"currency":"USD","positions":[{"ticker":"QQQ","quantity":2.0,"average_price":null}],"net_asset_value":null,"as_of_date":null,"reference_quotes":[]}'
    old = PortfolioContext.model_validate_json(old_json)
    assert old.fingerprint() == hashlib.sha256(old_json.encode()).hexdigest()[:12]


def test_forward_prices_costs_concurrency_and_restart(tmp_path):
    book, e, _ = setup(tmp_path)
    report = e.step()
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(book.record, [report, report]))
    prices = bars(n=390, slope=0)
    idx = prices.index
    prices.loc[idx[20], ["Open", "High", "Low", "Close"]] = [98, 98, 98, 98]
    prices.loc[idx[-1], ["Open", "High", "Low", "Close"]] = [102, 102, 102, 102]
    book.settle(STRATEGY, "TQQQ", prices, START)
    (row,) = book.recent(STRATEGY, "TQQQ")
    assert row["fill"] is None
    book.settle(STRATEGY, "TQQQ", prices, START.normalize() + pd.Timedelta(hours=16))
    (row,) = ForwardBook(tmp_path / "book.sqlite3").recent(STRATEGY, "TQQQ")
    assert row["qty"] == 35 and row["fill"] == 100
    assert row["after15_result"] == dict(avoided=70, missed=0, fees=5, net=65)
    assert row["closing_result"] == dict(avoided=0, missed=70, fees=5, net=-75)


def test_trend_dedup_rebuy_cost_and_independent_missing_mark(tmp_path):
    book, e, _ = setup(tmp_path)
    r = e.step()
    del r["opening"]
    r["strategy_id"] = "minute-trend-v1"
    book.record(r)
    for i, action, rule in [
        (1, "WAIT", "WATCH_RISK"),
        (2, "WATCH_RISK", "WATCH_RISK"),
        (4, "WATCH_LONG", "WATCH_LONG"),
    ]:
        at = START + pd.Timedelta(minutes=i)
        book.record(
            {
                **r,
                "action": action,
                "rule_action": rule,
                "generated_at": at.isoformat(),
                "snapshots": [{"bar_end": at.floor("min").isoformat()}],
            }
        )
    prices = bars(n=390, slope=0)
    prices.loc[prices.index[10], ["Open", "High", "Low", "Close"]] = [101, 101, 101, 101]
    close = START.normalize() + pd.Timedelta(hours=16)
    book.settle(r["strategy_id"], "TQQQ", prices.drop(prices.index[20]), close)
    (row,) = book.recent(r["strategy_id"], "TQQQ")
    assert row["closing_result"] == dict(avoided=0, missed=69, fees=10, net=-79)
    assert "after15_result" not in row
    book.settle(r["strategy_id"], "TQQQ", prices, close)
    assert book.recent(r["strategy_id"], "TQQQ")[0]["after15_result"] == row["closing_result"]


def test_early_close_and_late_signal_never_fill_overnight(tmp_path):
    book = ForwardBook(tmp_path / "book.sqlite3")
    at = pd.Timestamp("2026-11-27 12:55:10", tz="America/New_York")
    r = dict(
        ticker="QQQ",
        status="READY",
        action="WATCH_RISK",
        generated_at=at.isoformat(),
        snapshots=[{"bar_end": at.floor("min").isoformat()}],
    )
    book.record(r)
    book.settle(
        "minute-trend-v1",
        "QQQ",
        bars(n=210, date="2026-11-27", slope=0),
        at.replace(hour=13, minute=1),
    )
    (row,) = book.recent("minute-trend-v1", "QQQ")
    assert (
        row["short_horizon"] and "after15_result" not in row and row["closing_result"]["net"] == -5
    )
    book.record({**r, "ticker": "TQQQ", "generated_at": at.replace(minute=59).isoformat()})
    assert book.recent("minute-trend-v1", "TQQQ")[0]["gap"]
