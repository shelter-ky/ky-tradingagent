"""Run the installed agent's read-only public UAT check (no LLM billing/orders)."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from tradingagents.dataflows.intraday import IntradayDataError, market_session, utc_now
from tradingagents.dataflows.usmart_uat import (
    SOURCE, UatClient, UatError, UatMinuteProvider, frame_from_rows, market_timestamp, us_symbol,
)
from tradingagents.intraday import IntradayEngine


def no_paid_review(packet):
    raise IntradayDataError('本次为 UAT 数据与规则测试，尚未调用付费模型复核')


def evaluate_frames(ticker, frames, now, *, replay_at=None):
    """Use the genuine installed engine; a replay clock never becomes live time."""
    config = {'output_language':'Chinese'}
    provider = UatMinuteProvider(frames, lambda:now)
    live = IntradayEngine(ticker, config, fetch=provider, clock=lambda:now, review=no_paid_review).step()
    live['environment'] = 'HK UAT'
    live['mode'] = 'CURRENT_CLOCK_TEST'
    live['source'] = SOURCE
    live['limitations'] = [line for line in live['limitations'] if 'Yahoo' not in line]
    live['limitations'].append('公共 UAT 测试数据；不能据此执行真实交易。')
    live['provider_calls'] = list(provider.calls)
    validation = {}
    for symbol in frames:
        try:
            provider(symbol)
            validation[symbol] = {'status':'ACCEPTED_BY_EXISTING_DATA_VALIDATOR'}
        except IntradayDataError as exc:
            validation[symbol] = {'status':'BLOCKED', 'reason':str(exc)}
    result = {'current_clock':live, 'current_data_validation':validation}
    if replay_at is not None:
        replay_time = pd.Timestamp(replay_at)
        if replay_time.tz is None or replay_time > pd.Timestamp(now):
            raise ValueError('Replay time must include a timezone and cannot be in the future')
        visible_frames = {s:f.loc[f.index + pd.Timedelta(minutes=1) <= replay_time].copy()
                          for s,f in frames.items()}
        replay_provider = UatMinuteProvider(visible_frames, lambda:replay_time)
        replay = IntradayEngine(ticker, config, fetch=replay_provider, clock=lambda:replay_time, review=no_paid_review).step()
        replay.update(environment='HK UAT', mode='HISTORICAL_DATA_VALIDATION_ONLY', source=SOURCE,
                      real_trading_eligible=False, provider_calls=list(replay_provider.calls))
        replay['limitations'] = ['公共 UAT 旧数据回放，非实时信号；未调用模型、未下单。']
        result['historical_replay'] = replay
    return result


def run(demo_zip, output, ticker='TQQQ', replay_at=None):
    ticker = us_symbol(ticker)
    symbols = [ticker] + (['QQQ'] if ticker == 'TQQQ' else [])
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    client = UatClient(demo_zip)
    result = {'environment':'HK UAT / PUBLIC DEMO', 'agent_version':'0.7.1',
              'agent_module':str(Path(__file__).resolve()), 'ticker':ticker,
              'run_at':datetime.now(timezone.utc).isoformat(),
              'production_credentials_used':False, 'orders_sent':0, 'llm_calls':0,
              'timestamp_interpretation':'US market-local America/New_York; candle start labels, inferred from observed 09:30/15:59 bars; not independently certified',
              'real_trading_eligible':False}
    exit_code = 0
    try:
        client.login()
        result['login'] = 'PASS'
        quote_rows = client.quotes(symbols)
        result['quotes'] = [{k:row.get(k) for k in ('market','symbol','latestTime','latestPrice')} for row in quote_rows]
        result['test_holdings_rows'] = client.holdings_count()
        frames = {}
        result['candles'] = {}
        for symbol in symbols:
            rows = client.minute_rows(symbol)
            frame = frame_from_rows(rows)
            frames[symbol] = frame
            result['candles'][symbol] = {'rows':len(rows),
                'first_bar_utc':frame.index.min().isoformat() if len(frame) else None,
                'last_bar_utc':frame.index.max().isoformat() if len(frame) else None,
                'duplicates':int(frame.index.duplicated().sum()),
                'non_session_rows':sum(market_session(t) != 'REGULAR' for t in frame.index)}
        now = utc_now()
        result['quote_clock_check'] = [{
            'symbol':row['symbol'], 'time_utc':market_timestamp(row['latestTime']).isoformat(),
            'age_seconds':(pd.Timestamp(now)-market_timestamp(row['latestTime'])).total_seconds(),
            'fresh_for_current_regular_session':market_session(now) == 'REGULAR' and 0 <= (pd.Timestamp(now)-market_timestamp(row['latestTime'])).total_seconds() < 180,
        } for row in quote_rows]
        result.update(evaluate_frames(ticker, frames, now, replay_at=replay_at))
        result['transport_status'] = 'PASS'
        result['decision_test_status'] = 'COMPLETED_WITH_DATA_GATES'
    except (UatError, IntradayDataError, OSError, ValueError, TypeError, KeyError) as exc:
        exit_code = 1
        result.update(transport_status='INCOMPLETE', error_type=type(exc).__name__)
    finally:
        (output/'api-records-redacted.json').write_text(json.dumps(client.records, ensure_ascii=False, indent=2),encoding='utf-8')
        (output/'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2),encoding='utf-8')
    return result, exit_code


def main():
    parser = argparse.ArgumentParser(description='股票 agent 的 uSMART 公共 UAT 查询与数据校验；不发单、不调用付费模型')
    parser.add_argument('--demo-zip', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--ticker', default='TQQQ')
    parser.add_argument('--replay-at', help='Optional explicit historical ISO time with timezone; validation only')
    args = parser.parse_args()
    result, code = run(args.demo_zip, args.output, args.ticker, args.replay_at)
    print(json.dumps({k:result.get(k) for k in ('environment','login','transport_status','decision_test_status','error_type','orders_sent','llm_calls')},ensure_ascii=False,indent=2))
    for key in ('current_clock','historical_replay'):
        if key in result:
            print(key+': '+result[key]['action']+' / '+result[key]['reason'])
    print('结果目录：'+str(args.output.resolve()))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
