"""Thin Alpaca REST client. PAPER ONLY: the trading URL is hard-coded to the paper endpoint.

Every network call to Alpaca lives in this file, so if Alpaca changes something,
this is the only file that needs fixing.

Retries: GETs are retried on connection errors, timeouts, 429 and 5xx with backoff. POSTs and DELETEs are
never blindly retried (a retried order could double up); place.py and sync.py recover those by looking the
order up by its client_order_id instead.
"""
import logging
import os
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

PAPER_URL = "https://paper-api.alpaca.markets"   # never change this to the live URL
DATA_URL = "https://data.alpaca.markets"
TIMEOUT = 30
logging.getLogger("urllib3").setLevel(logging.ERROR)   # retry warnings print full URLs; keep them quiet


class BrokerError(RuntimeError):
    pass


class NotFound(BrokerError):
    pass


def retrying_session():
    s = requests.Session()
    s.trust_env = False      # ignore proxy / CA-bundle / .netrc settings from the environment: keys only go to Alpaca
    retry = Retry(total=4, connect=4, read=4, status=4, backoff_factor=1.5,
                  status_forcelist=(429, 500, 502, 503, 504), allowed_methods=frozenset({"GET"}),
                  respect_retry_after_header=True, raise_on_status=False)
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


class RestBroker:
    def __init__(self):
        key = os.environ.get("ALPACA_API_KEY")
        secret = os.environ.get("ALPACA_SECRET_KEY")
        if not key or not secret:
            raise BrokerError("Alpaca keys not found. Add them to the macOS Keychain (see README, 'Secrets'), "
                              "or as a fallback to socrates-desk/.env")
        self.s = retrying_session()
        self.s.headers.update({"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
        self.bars_feed = os.environ.get("ALPACA_BARS_FEED", "sip")    # free plan allows SIP history older than 15 min
        self.quote_feed = os.environ.get("ALPACA_QUOTE_FEED", "iex")  # free plan live quotes are IEX only

    # ---------- plumbing ----------
    def _req(self, method, url, **kw):
        for attempt in range(3):
            try:
                r = self.s.request(method, url, timeout=TIMEOUT, allow_redirects=False, **kw)
            except requests.RequestException as e:
                # GETs were already retried by the session; POST/DELETE are not retried here on purpose.
                raise BrokerError(f"{method} {url} -> network error: {type(e).__name__}: {e}") from e
            if r.status_code == 429 and method != "GET":   # rate limited: the request was not processed, safe to resend
                time.sleep(2 * (attempt + 1))
                continue
            if 300 <= r.status_code < 400:     # never follow a redirect: the key headers would go along with it
                raise BrokerError(f"{method} {url} -> unexpected redirect {r.status_code}")
            if r.status_code == 404:
                raise NotFound(f"{method} {url} -> 404: {r.text[:300]}")
            if r.status_code >= 400:
                raise BrokerError(f"{method} {url} -> {r.status_code}: {r.text[:300]}")
            return r.json() if r.text else {}
        raise BrokerError(f"{method} {url} -> rate limited 3 times")

    def _t(self, method, path, **kw):
        return self._req(method, PAPER_URL + path, **kw)

    def _d(self, path, **kw):
        return self._req("GET", DATA_URL + path, **kw)

    # ---------- account ----------
    def account(self):
        return self._t("GET", "/v2/account")

    def positions(self):
        return self._t("GET", "/v2/positions")

    def open_orders(self):
        return self._t("GET", "/v2/orders", params={"status": "open", "nested": "true", "limit": 500})

    def asset(self, symbol):
        return self._t("GET", f"/v2/assets/{symbol}")

    def clock(self):
        """{"is_open": bool, "next_open": ts, "next_close": ts, "timestamp": ts}"""
        return self._t("GET", "/v2/clock")

    def calendar_days(self, start, end):
        """Trading sessions between two YYYY-MM-DD dates (inclusive): [{"date","open","close"}, ...]."""
        return self._t("GET", "/v2/calendar", params={"start": start, "end": end})

    def calendar(self, start, end):
        """Trading days between two YYYY-MM-DD dates (inclusive)."""
        return [d["date"] for d in self.calendar_days(start, end)]

    def portfolio_history(self, period="1A"):
        return self._t("GET", "/v2/account/portfolio/history", params={"period": period, "timeframe": "1D"})

    # ---------- orders ----------
    def submit_bracket(self, symbol, qty, side, limit_price, take_profit, stop_price, client_order_id=None):
        body = {
            "symbol": symbol, "qty": str(qty), "side": side, "type": "limit",
            "time_in_force": "gtc", "limit_price": f"{limit_price:.2f}",
            "order_class": "bracket",
            "take_profit": {"limit_price": f"{take_profit:.2f}"},
            "stop_loss": {"stop_price": f"{stop_price:.2f}"},
        }
        if client_order_id:
            body["client_order_id"] = client_order_id
        return self._t("POST", "/v2/orders", json=body)

    def submit_oco(self, symbol, qty, side, take_profit, stop_price, client_order_id=None):
        """Exit-only OCO (take-profit limit + stop) for a position we already hold. side is the EXIT side."""
        body = {
            "symbol": symbol, "qty": str(qty), "side": side, "type": "limit", "time_in_force": "gtc",
            "order_class": "oco",
            "take_profit": {"limit_price": f"{take_profit:.2f}"},
            "stop_loss": {"stop_price": f"{stop_price:.2f}"},
        }
        if client_order_id:
            body["client_order_id"] = client_order_id
        return self._t("POST", "/v2/orders", json=body)

    def order(self, order_id):
        return self._t("GET", f"/v2/orders/{order_id}", params={"nested": "true"})

    def order_by_client_id(self, client_order_id):
        """The order with this client_order_id, or None if Alpaca has never seen it."""
        try:
            return self._t("GET", "/v2/orders:by_client_order_id",
                           params={"client_order_id": client_order_id, "nested": "true"})
        except NotFound:
            return None

    def cancel_order(self, order_id):
        return self._t("DELETE", f"/v2/orders/{order_id}")

    def close_position(self, symbol):
        return self._t("DELETE", f"/v2/positions/{symbol}")

    # ---------- market data ----------
    def daily_bars(self, symbols, start, end, adjustment="all"):
        """Returns {symbol: [ {t,o,h,l,c,v}, ... ]} oldest first. start/end are RFC3339 strings.
        adjustment: "all" (splits + dividends, for indicators), "split" (splits only, for simulating logged
        price levels forward) or "raw"."""
        for _ in range(2):
            feed = self.bars_feed
            out, token = {s: [] for s in symbols}, None
            try:
                while True:
                    params = {"symbols": ",".join(symbols), "timeframe": "1Day", "start": start, "end": end,
                              "limit": 10000, "adjustment": adjustment, "feed": feed}
                    if token:
                        params["page_token"] = token
                    j = self._d("/v2/stocks/bars", params=params)
                    for sym, bars in (j.get("bars") or {}).items():
                        out.setdefault(sym, []).extend(bars)
                    token = j.get("next_page_token")
                    if not token:
                        return out
            except BrokerError as e:
                if feed == "sip" and ("403" in str(e) or "subscription" in str(e).lower()):
                    self.bars_feed = "iex"      # plan refuses SIP: start over on IEX so pages never mix feeds
                    continue
                raise
        raise BrokerError("daily bars: no usable data feed")

    def latest_prices(self, symbols):
        """Returns {symbol: {"bid":x, "ask":y, "last":z}}."""
        q = self._d("/v2/stocks/quotes/latest", params={"symbols": ",".join(symbols), "feed": self.quote_feed}).get("quotes", {})
        t = self._d("/v2/stocks/trades/latest", params={"symbols": ",".join(symbols), "feed": self.quote_feed}).get("trades", {})
        out = {}
        for s in symbols:
            out[s] = {"bid": float(q.get(s, {}).get("bp") or 0), "ask": float(q.get(s, {}).get("ap") or 0),
                      "last": float(t.get(s, {}).get("p") or 0)}
        return out
