"""Thin Alpaca REST client. PAPER ONLY: the trading URL is hard-coded to the paper endpoint.

Every network call to Alpaca lives in this file, so if Alpaca changes something,
this is the only file that needs fixing.
"""
import os
import time
import requests

PAPER_URL = "https://paper-api.alpaca.markets"   # never change this to the live URL
DATA_URL = "https://data.alpaca.markets"


class BrokerError(RuntimeError):
    pass


class RestBroker:
    def __init__(self):
        key = os.environ.get("ALPACA_API_KEY")
        secret = os.environ.get("ALPACA_SECRET_KEY")
        if not key or not secret:
            raise BrokerError("Alpaca keys not found. Add them to the macOS Keychain (see README, 'Secrets'), "
                              "or as a fallback to socrates-desk/.env")
        self.s = requests.Session()
        self.s.headers.update({"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
        self.bars_feed = os.environ.get("ALPACA_BARS_FEED", "sip")    # free plan allows SIP history older than 15 min
        self.quote_feed = os.environ.get("ALPACA_QUOTE_FEED", "iex")  # free plan live quotes are IEX only

    # ---------- plumbing ----------
    def _req(self, method, url, **kw):
        for attempt in range(3):
            r = self.s.request(method, url, timeout=30, **kw)
            if r.status_code == 429:          # rate limited: back off and retry
                time.sleep(2 * (attempt + 1))
                continue
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

    def calendar(self, start, end):
        """Trading days between two YYYY-MM-DD dates (inclusive)."""
        return [d["date"] for d in self._t("GET", "/v2/calendar", params={"start": start, "end": end})]

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

    def order(self, order_id):
        return self._t("GET", f"/v2/orders/{order_id}", params={"nested": "true"})

    def cancel_order(self, order_id):
        return self._t("DELETE", f"/v2/orders/{order_id}")

    def close_position(self, symbol):
        return self._t("DELETE", f"/v2/positions/{symbol}")

    # ---------- market data ----------
    def daily_bars(self, symbols, start, end):
        """Returns {symbol: [ {t,o,h,l,c,v}, ... ]} oldest first. start/end are RFC3339 strings."""
        out, token = {s: [] for s in symbols}, None
        while True:
            params = {"symbols": ",".join(symbols), "timeframe": "1Day", "start": start, "end": end,
                      "limit": 10000, "adjustment": "all", "feed": self.bars_feed}
            if token:
                params["page_token"] = token
            try:
                j = self._d("/v2/stocks/bars", params=params)
            except BrokerError as e:
                if self.bars_feed == "sip" and ("403" in str(e) or "subscription" in str(e).lower()):
                    self.bars_feed = "iex"      # fall back if the plan refuses SIP
                    continue
                raise
            for sym, bars in (j.get("bars") or {}).items():
                out.setdefault(sym, []).extend(bars)
            token = j.get("next_page_token")
            if not token:
                return out

    def latest_prices(self, symbols):
        """Returns {symbol: {"bid":x, "ask":y, "last":z}}."""
        q = self._d("/v2/stocks/quotes/latest", params={"symbols": ",".join(symbols), "feed": self.quote_feed}).get("quotes", {})
        t = self._d("/v2/stocks/trades/latest", params={"symbols": ",".join(symbols), "feed": self.quote_feed}).get("trades", {})
        out = {}
        for s in symbols:
            out[s] = {"bid": float(q.get(s, {}).get("bp") or 0), "ask": float(q.get(s, {}).get("ap") or 0),
                      "last": float(t.get(s, {}).get("p") or 0)}
        return out
