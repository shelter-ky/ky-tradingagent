"""Read-only public HK UAT adapter. No production host or order endpoints.

Protocol: https://api-doc.usmart8.com/zh-cn/quote-base.html and official Python demo.
Market-clock interpretation (US local time, candle-start labels) is explicit in
the test report; passing a transport check never approves data for real trading.
"""
from __future__ import annotations

import base64
import json
import secrets
import time
import zipfile
from dataclasses import replace
from datetime import datetime, timezone

import pandas as pd
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from tradingagents.dataflows.intraday import IntradayDataError, snapshot_from_bars, snapshots_from_pair

LOGIN = '/user-server/open-api/login'
QUOTES = '/quotes-openservice/api/v1/realtime'
KLINE = '/quotes-openservice/api/v1/kline'
HOLDINGS = '/stock-order-server/open-api/stock-holding'
TRADE_HOST = 'https://open-jy-uat.yxzq.com'
QUOTE_HOST = 'https://open-hz-uat.yxzq.com'
SOURCE = 'uSMART HK UAT / public demo / unadjusted 1m / TEST DATA'


class UatError(RuntimeError):
    """Errors contain endpoint/code/type only, never tokens or credentials."""


def endpoint_url(endpoint):
    if endpoint in (LOGIN, HOLDINGS):
        return TRADE_HOST + endpoint
    if endpoint in (QUOTES, KLINE):
        return QUOTE_HOST + endpoint
    raise UatError('Endpoint is outside the read-only UAT allowlist')


def us_symbol(value):
    value = value.strip().upper()
    if not value or len(value) > 20 or any(c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-' for c in value):
        raise UatError('Invalid US ticker')
    return value


def market_timestamp(value):
    text = str(value)
    if isinstance(value, bool) or len(text) != 17 or not text.isdigit():
        raise IntradayDataError('UAT 行情时间格式不明确')
    try:
        dt = datetime.strptime(text, '%Y%m%d%H%M%S%f')
        return pd.Timestamp(dt).tz_localize('America/New_York', ambiguous='raise', nonexistent='raise').tz_convert('UTC')
    except (ValueError, TypeError) as exc:
        raise IntradayDataError('UAT 行情时间无效') from exc


def frame_from_rows(rows):
    """Preserve timestamps and gaps; do not interpolate or invent candles."""
    names = {'Open':'open', 'High':'high', 'Low':'low', 'Close':'close', 'Volume':'volume'}
    if not isinstance(rows, list):
        raise IntradayDataError('UAT 分钟线结构无效')
    if not rows:
        return pd.DataFrame(columns=list(names), index=pd.DatetimeIndex([], tz='UTC'))
    try:
        if any(isinstance(row[key], bool) for row in rows for key in names.values()):
            raise ValueError('boolean candle')
        frame = pd.DataFrame({label:[row[key] for row in rows] for label,key in names.items()},
                             index=pd.DatetimeIndex([market_timestamp(row['latestTime']) for row in rows]))
    except (KeyError, TypeError, ValueError) as exc:
        raise IntradayDataError('UAT 分钟线字段不完整或无效') from exc
    return frame


class UatClient:
    def __init__(self, demo_zip, *, session=None):
        with zipfile.ZipFile(demo_zip) as archive:
            user = json.loads(archive.read('openapi-demo-py/conf/config.json'))['default_user']
        self._public_key = serialization.load_der_public_key(base64.b64decode(user['public_key']))
        self._private_key = serialization.load_der_private_key(base64.b64decode(user['private_key']), password=None)
        self._password = user['login_password']
        self._channel, self._lang = str(user['X-Channel']), str(user['X-Lang'])
        self._token = None
        self._session = session if session is not None else requests.Session()
        self.records = []

    def _encrypt(self, text):
        return base64.urlsafe_b64encode(self._public_key.encrypt(text.encode(), padding.PKCS1v15())).decode()

    def _sign(self, text, quote):
        raw = self._private_key.sign(text.encode(), padding.PKCS1v15(), hashes.MD5())
        return (base64.urlsafe_b64encode if quote else base64.b64encode)(raw).decode()

    def request(self, endpoint, params):
        url = endpoint_url(endpoint)  # Validate before constructing any auth headers.
        if endpoint != LOGIN and not self._token:
            raise UatError('Login is required')
        body = json.dumps(params)
        quote = endpoint in (QUOTES, KLINE)
        headers = {'Content-Type':'application/json; charset=utf-8', 'X-Lang':self._lang, 'X-Channel':self._channel}
        if self._token:
            headers['Authorization'] = self._token
        if quote:
            headers['X-Request-Id'] = str(time.time_ns())
            headers['X-Time'] = str(int(time.time()))
            content = self._token + self._channel + self._lang + headers['X-Request-Id'] + headers['X-Time'] + body
        else:
            content = body
        headers['X-Sign'] = self._sign(content, quote)
        record = {'time':datetime.now(timezone.utc).isoformat(), 'environment':'HK UAT', 'endpoint':endpoint}
        if endpoint in (QUOTES, KLINE):
            record['request'] = params
        try:
            response = self._session.post(url, data=body.encode(), headers=headers,
                                          timeout=(10, 20), allow_redirects=False)
            record['http_status'] = response.status_code
            if response.status_code != 200:
                raise UatError(f'UAT HTTP status {response.status_code}')
            result = response.json()
            if not isinstance(result, dict):
                raise UatError('Invalid UAT response')
            record['code'] = result.get('code')
            if result.get('code') != 0:
                raise UatError(f'UAT API code {result.get("code")}')
            if endpoint == LOGIN:
                record['token_received'] = bool((result.get('data') or {}).get('token'))
            elif endpoint in (QUOTES, KLINE):
                record['response'] = result
            else:
                record['row_count'] = len(result.get('data', [])) if isinstance(result.get('data'), list) else None
            return result.get('data')
        except (requests.RequestException, ValueError) as exc:
            record['error_type'] = type(exc).__name__
            raise UatError('UAT request failed: '+type(exc).__name__) from None
        finally:
            self.records.append(record)

    def login(self):
        # This is the previously verified administrator-published PUBLIC HK UAT account.
        data = self.request(LOGIN, {'phoneNumber':self._encrypt('90117052'),
                                  'password':self._encrypt(self._password), 'areaCode':'852'})
        self._token = (data or {}).get('token')
        if not isinstance(self._token, str) or not self._token:
            raise UatError('UAT login returned no token')

    def quotes(self, tickers):
        symbols = [us_symbol(t) for t in tickers]
        data = self.request(QUOTES, {'secuIds':['us'+s for s in symbols]})
        rows = data.get('list', []) if isinstance(data, dict) else []
        if len(rows) != len(symbols) or {r.get('symbol') for r in rows} != set(symbols) or any(r.get('market') != 'us' for r in rows):
            raise UatError('UAT quote instrument identity mismatch')
        return rows

    def minute_rows(self, ticker):
        data = self.request(KLINE, {'secuId':'us'+us_symbol(ticker), 'type':1, 'start':0, 'right':0, 'count':300})
        if not isinstance(data, dict) or not isinstance(data.get('list'), list):
            raise UatError('UAT candle payload is invalid')
        return data['list']

    def holdings_count(self):
        data = self.request(HOLDINGS, {'exchangeType':'5'})
        if not isinstance(data, list):
            raise UatError('UAT holdings payload is invalid')
        return len(data)


class UatMinuteProvider:
    def __init__(self, frames, clock):
        self.frames, self.clock = frames, clock
        self.calls = []

    def __call__(self, ticker):
        self.calls.append(ticker)
        if ticker not in self.frames:
            raise IntradayDataError('UAT 标的分钟线未提供')
        snap = snapshot_from_bars(ticker, self.frames[ticker], self.clock())
        return replace(snap, source=SOURCE)

    def paired(self, ticker, proxy):
        self.calls.extend((ticker, proxy))
        if ticker not in self.frames or proxy not in self.frames:
            raise IntradayDataError("UAT 双标的分钟线未提供")
        aligned=snapshots_from_pair({symbol:self.frames[symbol] for symbol in (ticker,proxy)},self.clock())
        return [replace(snap,source=SOURCE) for snap in aligned]
