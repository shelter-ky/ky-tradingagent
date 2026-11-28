"""Local forward event study. Each sale uses its own fixed reference book, never broker holdings."""

from contextlib import closing, contextmanager
import json
import math
from pathlib import Path
import sqlite3

import pandas as pd

from tradingagents.dataflows.intraday import aware_utc
from tradingagents.dataflows.vendors.yahoo.sessions import NY_TZ, session_close
from tradingagents.opening_rule import ORDER_FEE, REFERENCE_CAPITAL


def stamp(value):
    return aware_utc(value).isoformat()


def next_open(value):
    return aware_utc(value).floor("min") + pd.Timedelta(minutes=1)


class ForwardBook:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS observations(
                strategy TEXT, ticker TEXT, day TEXT, at TEXT, side TEXT, target REAL,
                bar_end TEXT, PRIMARY KEY(strategy,ticker,day))""")
            if "bar_end" not in {r["name"] for r in db.execute("PRAGMA table_info(observations)")}:
                db.execute("ALTER TABLE observations ADD COLUMN bar_end TEXT")
            db.execute("""CREATE TABLE IF NOT EXISTS opening_decisions(
                strategy TEXT,ticker TEXT,day TEXT,payload TEXT,PRIMARY KEY(strategy,ticker,day))""")
            db.execute("""CREATE TABLE IF NOT EXISTS sales(
                id INTEGER PRIMARY KEY, strategy TEXT, ticker TEXT, day TEXT, signal TEXT,
                bar_end TEXT, previous REAL, target REAL, earliest TEXT, buy_signal TEXT,
                fill REAL, qty INTEGER, buy_price REAL, after15 REAL, closing REAL,
                gap TEXT, done INTEGER NOT NULL DEFAULT 0, UNIQUE(strategy,ticker,signal))""")
            if "done" not in {r["name"] for r in db.execute("PRAGMA table_info(sales)")}:
                db.execute("ALTER TABLE sales ADD COLUMN done INTEGER NOT NULL DEFAULT 0")

    @contextmanager
    def transaction(self):
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            yield db

    def prior_target(self, strategy, ticker, day):
        with self.transaction() as db:
            row = db.execute(
                "SELECT payload FROM opening_decisions WHERE strategy=? AND ticker=? AND day<? "
                "ORDER BY day DESC LIMIT 1",
                (strategy, ticker, day),
            ).fetchone()
        return 1.0 if row is None else json.loads(row["payload"])["opening"]["target"]

    def opening_decision(self, strategy, ticker, day, new=None):
        with self.transaction() as db:
            if new is not None:
                public = {
                    key: new[key]
                    for key in (
                        "generated_at",
                        "valid_until",
                        "reason",
                        "action",
                        "snapshots",
                        "opening",
                        "premarket_context",
                    )
                }
                db.execute(
                    "INSERT OR IGNORE INTO opening_decisions VALUES(?,?,?,?)",
                    (strategy, ticker, day, json.dumps(public, allow_nan=False)),
                )
            row = db.execute(
                "SELECT payload FROM opening_decisions WHERE strategy=? AND ticker=? AND day=?",
                (strategy, ticker, day),
            ).fetchone()
            return None if row is None else json.loads(row["payload"])

    def record(self, report):
        if report["status"] != "READY" or not report["snapshots"]:
            return
        strategy = report.get("strategy_id", "minute-trend-v1")
        ticker, at = report["ticker"], aware_utc(report.get("published_at", report["generated_at"]))
        bar_end = aware_utc(report["snapshots"][0]["bar_end"])
        if at < aware_utc(report["generated_at"]) or at < bar_end:
            return
        day = str(at.tz_convert(NY_TZ).date())
        action = report["action"]
        side = report.get("rule_action", action)
        opening = "opening" in report
        previous, target = (
            (report["opening"]["previous"], report["opening"]["target"]) if opening else (1.0, 0.0)
        )
        key = (strategy, ticker, day)
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM observations WHERE strategy=? AND ticker=? AND day=?", key
            ).fetchone()
            if row and (
                opening
                or at <= aware_utc(row["at"])
                or (row["bar_end"] and bar_end <= aware_utc(row["bar_end"]))
            ):
                return
            # A model veto during a continuing technical risk run cannot re-arm a recorded sale.
            fired = action == "WATCH_RISK" or (
                side == "WATCH_RISK" and row and row["side"] == "SOLD"
            )
            if action == "WATCH_RISK" and (not row or row["side"] != "SOLD"):
                eligible = next_open(at)
                gap = "信号后当日没有完整成交分钟" if eligible >= session_close(day) else None
                db.execute(
                    """INSERT INTO sales(strategy,ticker,day,signal,bar_end,previous,target,earliest,gap)
                           VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        *key,
                        stamp(at),
                        report["snapshots"][0]["bar_end"],
                        previous,
                        target,
                        stamp(eligible),
                        gap,
                    ),
                )
            if action == "WATCH_LONG":
                db.execute(
                    "UPDATE sales SET buy_signal=? WHERE strategy=? AND ticker=? AND day=? "
                    "AND buy_signal IS NULL AND done=0 AND gap IS NULL",
                    (stamp(at), *key),
                )
            db.execute(
                "INSERT OR REPLACE INTO observations(strategy,ticker,day,at,side,target,bar_end) VALUES(?,?,?,?,?,?,?)",
                (
                    *key,
                    stamp(at),
                    "SOLD" if fired else side,
                    target if opening else None,
                    stamp(bar_end),
                ),
            )

    def pending(self, strategy, ticker):
        with self.transaction() as db:
            return bool(
                db.execute(
                    "SELECT 1 FROM sales WHERE strategy=? AND ticker=? AND done=0 "
                    "AND gap IS NULL LIMIT 1",
                    (strategy, ticker),
                ).fetchone()
            )

    def settle(self, strategy, ticker, frame, now):
        now = aware_utc(now)
        if frame.empty or not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None:
            return

        def price(start, field):
            if start + pd.Timedelta(minutes=1) > now or start not in frame.index:
                return None
            row = frame.loc[start]
            if isinstance(row, pd.DataFrame):
                return None
            values = [float(row[k]) for k in ("Open", "High", "Low", "Close", "Volume")]
            o, h, l, c, v = values
            if not all(math.isfinite(x) for x in values) or not (
                0 < l <= min(o, c) <= max(o, c) <= h and v > 0
            ):
                return None
            return o if field == "Open" else c

        with self.transaction() as db:
            rows = db.execute(
                "SELECT * FROM sales WHERE strategy=? AND ticker=? AND done=0 AND gap IS NULL",
                (strategy, ticker),
            ).fetchall()
            for row in rows:
                start = aware_utc(row["earliest"])
                close = session_close(row["day"])
                fill = row["fill"] or price(start, "Open")
                if fill is not None:
                    qty = row["qty"]
                    if qty is None:
                        qty = math.floor(
                            (REFERENCE_CAPITAL - ORDER_FEE) * row["previous"] / fill
                        ) - math.floor((REFERENCE_CAPITAL - ORDER_FEE) * row["target"] / fill)
                    buy = row["buy_price"]
                    if row["buy_signal"] and next_open(row["buy_signal"]) < close and buy is None:
                        buy = price(next_open(row["buy_signal"]), "Open")
                    calendar15 = start + pd.Timedelta(minutes=15)
                    after15 = row["after15"]
                    if after15 is None and calendar15 <= close:
                        after15 = price(calendar15 - pd.Timedelta(minutes=1), "Close")
                    closing = row["closing"] or price(close - pd.Timedelta(minutes=1), "Close")
                    buy_due = bool(row["buy_signal"] and next_open(row["buy_signal"]) < close)
                    done = (
                        closing is not None
                        and (calendar15 > close or after15 is not None)
                        and (not buy_due or buy is not None)
                    )
                    db.execute(
                        "UPDATE sales SET fill=?,qty=?,buy_price=?,after15=?,closing=?,gap=?,done=? WHERE id=?",
                        (
                            fill,
                            qty,
                            buy,
                            after15,
                            closing,
                            "模拟减仓不足一股" if qty <= 0 else None,
                            done,
                            row["id"],
                        ),
                    )
                if now > close + pd.Timedelta(days=7):
                    db.execute(
                        "UPDATE sales SET gap='缺少指定分钟，无法核对' WHERE id=? AND done=0 AND gap IS NULL",
                        (row["id"],),
                    )

    def recent(self, strategy, ticker, limit=20):
        with self.transaction() as db:
            rows = db.execute(
                "SELECT * FROM sales WHERE strategy=? AND ticker=? ORDER BY signal DESC LIMIT ?",
                (strategy, ticker, limit),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["short_horizon"] = aware_utc(row["earliest"]) + pd.Timedelta(
                minutes=15
            ) > session_close(row["day"])
            for name, end in (
                ("after15", aware_utc(row["earliest"]) + pd.Timedelta(minutes=15)),
                ("closing", session_close(row["day"])),
            ):
                mark = row[name]
                if mark is None or not row["qty"]:
                    continue
                buyback = row["buy_signal"] and next_open(row["buy_signal"]) < end
                exit_price = row["buy_price"] if buyback else mark
                if exit_price is None:
                    continue
                gross = row["qty"] * (row["fill"] - exit_price)
                fee = ORDER_FEE * (2 if buyback else 1)
                item[name + "_result"] = dict(
                    avoided=max(gross, 0), missed=max(-gross, 0), fees=fee, net=gross - fee
                )
            result.append(item)
        return result
