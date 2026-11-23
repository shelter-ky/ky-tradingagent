"""Offline boundaries for the optional read-only public UAT adapter."""
import unittest
from unittest.mock import Mock

import pandas as pd

from tradingagents.dataflows.intraday import IntradayDataError
from tradingagents.dataflows.usmart_uat import (
    KLINE, QUOTES, SOURCE, UatClient, UatError, UatMinuteProvider,
    endpoint_url, frame_from_rows, market_timestamp,
)
from tradingagents.uat_check import evaluate_frames


def bars(day='2026-11-20', count=30):
    index = pd.date_range(day+' 09:30', periods=count, freq='min', tz='America/New_York').tz_convert('UTC')
    return pd.DataFrame({'Open':[100.]*count, 'High':[101.]*count,
                         'Low':[99.]*count, 'Close':[100.]*count, 'Volume':[100.]*count},index=index)


class UatBoundaryTests(unittest.TestCase):
    def test_production_and_order_routes_never_reach_transport(self):
        client = object.__new__(UatClient)
        for endpoint in ('https://open-hz.yxzq.com:8443'+QUOTES,
                         '/stock-order-server/open-api/entrust-order',
                         '/stock-order-server/open-api/modify-order',
                         '/user-server/open-api/trade-login'):
            with self.subTest(endpoint=endpoint), self.assertRaises(UatError):
                client.request(endpoint,{})
        self.assertEqual(endpoint_url(KLINE),'https://open-hz-uat.yxzq.com'+KLINE)

    def test_source_timestamp_is_not_refreshed(self):
        self.assertEqual(market_timestamp(20261120093000000).isoformat(),'2026-11-20T14:30:00+00:00')
        with self.assertRaises(IntradayDataError):
            market_timestamp(True)

    def test_rows_remain_sparse(self):
        rows = [dict(latestTime=t,open=1,high=2,low=1,close=2,volume=10)
                for t in (20261120093000000,20261120155900000)]
        frame = frame_from_rows(rows)
        self.assertEqual(len(frame),2)
        self.assertEqual((frame.index[1]-frame.index[0]).total_seconds(),389*60)

    def test_duplicated_and_gapped_bars_rejected_by_agent(self):
        base = bars()
        for frame in (pd.concat([base,base.iloc[-1:]]),base.drop(base.index[4])):
            with self.subTest(rows=len(frame)), self.assertRaises(IntradayDataError):
                UatMinuteProvider({'AAPL':frame},lambda:pd.Timestamp('2026-11-20 15:00Z'))('AAPL')

    def test_valid_snapshot_keeps_uat_identity(self):
        snap = UatMinuteProvider({'AAPL':bars()},lambda:pd.Timestamp('2026-11-20 15:00Z'))('AAPL')
        self.assertEqual(snap.source,SOURCE)
        self.assertEqual(snap.bar_end,'2026-11-20T15:00:00+00:00')

    def test_old_quotes_cannot_form_live_signals(self):
        result = evaluate_frames('TQQQ',{'TQQQ':bars(),'QQQ':bars()},pd.Timestamp('2026-11-23 15:00Z'))
        live = result['current_clock']
        self.assertEqual(live['action'],'WAIT')
        self.assertEqual(live['status'],'UNAVAILABLE')
        self.assertIsNone(live['sizing']['validated_allocation'])
        self.assertEqual(live['model_status'],'NOT_CALLED')

    def test_current_session_checks_use_supplied_data_no_yahoo(self):
        result = evaluate_frames('TQQQ',{'TQQQ':bars(),'QQQ':bars()},pd.Timestamp('2026-11-20 15:00Z'))
        live = result['current_clock']
        self.assertEqual(live['status'],'READY')
        self.assertEqual(live['action'],'WAIT')
        self.assertEqual(live['provider_calls'],['TQQQ','QQQ'])
        self.assertTrue(all('UAT' in s['source'] for s in live['snapshots']))
        self.assertEqual(live['model_status'],'SKIPPED_MIXED_SIGNALS')

    def test_replay_excludes_future_candles_and_never_appears_live(self):
        frame = bars(count=60)
        frames = {'TQQQ':frame,'QQQ':frame}
        result = evaluate_frames('TQQQ',frames,pd.Timestamp('2026-11-23 15:00Z'),replay_at='2026-11-20T15:00:00Z')
        replay = result['historical_replay']
        self.assertEqual(replay['status'],'READY')
        self.assertEqual(replay['snapshots'][0]['bars'],30)
        self.assertFalse(replay['real_trading_eligible'])
        self.assertEqual(replay['mode'],'HISTORICAL_DATA_VALIDATION_ONLY')

    def test_bad_instrument_response_is_rejected(self):
        client = object.__new__(UatClient)
        client.request = Mock(return_value={'list':[{'symbol':'QQQ','market':'hk'}]})
        with self.assertRaises(UatError):
            client.quotes(['QQQ'])

    def test_no_redirects_or_token_in_saved_market_record(self):
        client = object.__new__(UatClient)
        client._token = 'test-token-secret'
        client._channel, client._lang = 'test','1'
        client._sign = Mock(return_value='test-signature-secret')
        client._session = Mock()
        client._session.post.return_value.status_code = 200
        client._session.post.return_value.json.return_value = {'code':0,'data':{'list':[]}}
        client.records = []
        client.request(KLINE,{'secuId':'usQQQ'})
        self.assertFalse(client._session.post.call_args.kwargs['allow_redirects'])
        self.assertNotIn('test-token-secret',repr(client.records))
        self.assertNotIn('test-signature-secret',repr(client.records))


if __name__ == '__main__':
    unittest.main()
