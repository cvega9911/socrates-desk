"""Offline tests: a fake Alpaca with synthetic prices exercises score -> place -> sync -> review.
Run:  python tests/test_desk.py
"""
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np

TMP = Path(tempfile.mkdtemp())
os.environ["DESK_ROOT"] = str(TMP)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
(TMP / "journal").mkdir()
(TMP / "reports").mkdir()
(TMP / "rules_version.txt").write_text("v1.0\n")
(TMP / "start_date.txt").write_text("PENDING\n")

import desk as D          # noqa: E402
import broker as BR       # noqa: E402
import score, place, sync, review, control   # noqa: E402

TODAY = dt.date.fromisoformat(D.today_str())
place.sleep = sync.sleep = lambda s: None     # no real waiting in tests


def bdays(n_back, n_fwd=0):
    days, d = [], TODAY
    while len(days) < n_back:
        d -= dt.timedelta(days=1)
        if d.weekday() < 5:
            days.append(d)
    days = sorted(days)
    d = TODAY
    for _ in range(n_fwd):
        if d.weekday() < 5:
            days.append(d)
        d += dt.timedelta(days=1)
    return days


DRIFT = {"SPY": 0.001, "XLE": 0.004, "XOM": 0.004, "USO": 0.004, "TLT": -0.004, "IEF": -0.003,
         "GDX": -0.005, "SLV": -0.005, "QQQ": -0.001}


class FakeBroker:
    def __init__(self, n_bars=100, flat=False, seed=1):
        rng = np.random.default_rng(seed)
        self.days = bdays(n_bars)
        self.bars = {}
        for s in D.UNIVERSE:
            drift = 0.0 if flat else DRIFT.get(s, 0.0005)
            px, rows = 100.0, []
            for i, d in enumerate(self.days):
                px *= 1 + drift + rng.normal(0, 0.006 if not flat else 0.002)
                # a pullback-free trend with a 3-day dip 6 bars ago gives stops room
                hi, lo = px * 1.008, px * 0.992
                vol = 1_000_000 * (1.5 if i == len(self.days) - 1 else 1.0)
                rows.append({"t": f"{d.isoformat()}T04:00:00Z", "o": px, "h": hi, "l": lo, "c": px, "v": vol})
            self.bars[s] = rows
        self.equity, self.last_equity = 100000.0, 100000.0
        self.orders, self.pos, self.n = {}, {}, 0
        self.cancelled = []
        self.is_open, self.holidays = True, set()
        self.submit_mode = None        # None | "timeout_after_accept" | "error" | "reject"
        self.close_fails = False
        self.cancel_stuck = False      # cancels are accepted but never take effect
        self.calendar_fails = False
        self.order_fails = set()
        self.close_lost_reply = False
        self.ocos = []

    def clock(self):
        return {"is_open": self.is_open}

    def calendar_days(self, a, b):
        if self.calendar_fails:
            raise BR.BrokerError("calendar down")
        a, b = dt.date.fromisoformat(a), dt.date.fromisoformat(b)
        out = []
        for i in range((b - a).days + 1):
            d = a + dt.timedelta(days=i)
            if (d.weekday() < 5 or d == TODAY) and d.isoformat() not in self.holidays:
                out.append({"date": d.isoformat(), "open": "09:30", "close": "16:00"})
        return out

    def order_by_client_id(self, cid):
        return next((o for o in self.orders.values() if o.get("client_order_id") == cid), None)

    def account(self):
        return {"equity": str(self.equity), "last_equity": str(self.last_equity)}

    def positions(self):
        return list(self.pos.values())

    def open_orders(self):
        return [o for o in self.orders.values() if o["status"] in ("new", "accepted")]

    def asset(self, s):
        return {"shortable": True, "easy_to_borrow": s != "GDX"}

    def calendar(self, a, b):
        return [d["date"] for d in self.calendar_days(a, b)]

    def portfolio_history(self, period="1A"):
        return {"equity": [], "timestamp": []}

    def submit_bracket(self, symbol, qty, side, limit_price, take_profit, stop_price, client_order_id=None):
        if self.submit_mode == "error":
            raise BR.BrokerError("POST /v2/orders -> 422: insufficient buying power", 422)
        if self.submit_mode == "error500":
            raise BR.BrokerError("POST /v2/orders -> 500: boom", 500)
        self.n += 1
        oid = f"ord{self.n}"
        self.orders[oid] = {"id": oid, "symbol": symbol, "qty": qty, "side": side,
                            "status": "rejected" if self.submit_mode == "reject" else "new",
                            "limit": limit_price, "client_order_id": client_order_id, "filled_qty": "0",
                            "legs": [{"id": oid + "tp", "type": "limit", "status": "held", "limit_price": take_profit},
                                     {"id": oid + "sl", "type": "stop", "status": "held", "stop_price": stop_price}]}
        if self.submit_mode == "timeout_after_accept":
            raise BR.BrokerError("POST /v2/orders -> network error: ReadTimeout")
        return {"id": oid}

    def submit_oco(self, symbol, qty, side, take_profit, stop_price, client_order_id=None):
        self.n += 1
        oid = f"oco{self.n}"
        self.orders[oid] = {"id": oid, "symbol": symbol, "qty": qty, "side": side, "type": "limit", "status": "new",
                            "client_order_id": client_order_id, "limit_price": take_profit,
                            "legs": [{"id": oid + "sl", "type": "stop", "status": "new", "stop_price": stop_price}]}
        self.ocos.append(self.orders[oid])
        return {"id": oid}

    def order(self, oid):
        if oid in self.order_fails:
            raise BR.BrokerError(f"GET order {oid} -> 500")
        if oid in self.orders:
            return self.orders[oid]
        for o in self.orders.values():
            for l in o.get("legs") or []:
                if l["id"] == oid:
                    return l
        raise BR.NotFound(f"order {oid} not found")

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        if self.cancel_stuck:
            return
        for o in self.orders.values():
            if o["id"] == oid and o["status"] not in ("filled",):
                o["status"] = "canceled"
                for l in o.get("legs") or []:
                    if l["status"] in ("held", "new"):
                        l["status"] = "canceled"
            for l in o.get("legs") or []:
                if l["id"] == oid and l["status"] != "filled":
                    l["status"] = "canceled"

    def close_position(self, s):
        if self.close_lost_reply:          # the sale happens, the reply never arrives
            self.close_lost_reply = False
            self.n += 1
            oid = f"close{self.n}"
            px = float(self.pos[s]["current_price"])
            self.orders[oid] = {"id": oid, "symbol": s, "side": "sell", "status": "filled", "filled_avg_price": str(px),
                                "filled_at": f"{TODAY}T19:55:00Z", "legs": []}
            del self.pos[s]
            raise BR.BrokerError("DELETE /v2/positions -> network error: ReadTimeout")
        if self.close_fails:
            raise BR.BrokerError(f"DELETE /v2/positions/{s} -> 403: insufficient qty available")
        if s not in self.pos:
            raise BR.NotFound(f"position {s} not found")
        self.n += 1
        oid = f"close{self.n}"
        px = float(self.pos[s]["current_price"])
        self.orders[oid] = {"id": oid, "symbol": s, "side": "sell", "status": "filled", "filled_avg_price": str(px),
                            "filled_at": f"{TODAY}T19:55:00Z", "legs": []}
        del self.pos[s]
        return {"id": oid}

    def recent_orders(self, symbol, after):
        return [o for o in self.orders.values() if o.get("symbol") == symbol and o.get("status") == "filled"
                and str(o["id"]).startswith("close")]

    def daily_bars(self, symbols, start, end, adjustment="all"):
        return {s: self.bars[s] for s in symbols if s in self.bars}

    def latest_prices(self, symbols):
        out = {}
        for s in symbols:
            c = self.bars[s][-1]["c"]
            out[s] = {"bid": c * 0.9999, "ask": c * 1.0001, "last": c}
        return out

    # helpers for tests
    def fill_entry(self, oid, price):
        o = self.orders[oid]
        o.update({"status": "filled", "filled_avg_price": str(price), "filled_at": f"{TODAY}T14:20:00Z",
                  "filled_qty": str(o["qty"])})
        for l in o["legs"]:
            l["status"] = "new"
        self.pos[o["symbol"]] = {"symbol": o["symbol"], "current_price": str(price)}

    def fill_leg(self, oid, which, price):
        o = self.orders[oid]
        for l in o["legs"]:
            l["status"] = "filled" if l["type"] == which else "canceled"
            if l["type"] == which:
                l.update({"filled_avg_price": str(price), "filled_at": f"{TODAY}T18:00:00Z"})
        self.pos.pop(o["symbol"], None)


def reset_journal():
    for f in ("candidates.jsonl", "trades.jsonl", "weekly.md", "control.jsonl"):
        (TMP / "journal" / f).write_text("")
    for f in (TMP / "reports").glob("*"):
        f.unlink()


passed = 0


def ok(cond, msg):
    global passed
    if not cond:
        raise AssertionError(msg)
    passed += 1
    print("  ok -", msg)


# ---------------- unit math ----------------
print("unit math")
f = {"atr": 2.0, "low10": 97.0, "high10": 103.0, "high20": 104.0, "low20": 96.0, "high60": 110.0, "low60": 90.0}
stop, tgt, rr = D.levels(f, 100.0, "long")
ok(stop == 97.0 and tgt == 104.0 and rr == 1.33, f"long levels stop 97 target 104 rr 1.33 (got {stop},{tgt},{rr})")
f2 = dict(f, low10=99.5)
stop, tgt, rr = D.levels(f2, 100.0, "long")
ok(stop == 98.0, f"stop clamped to at least 1 ATR (got {stop})")
f3 = dict(f, low10=90.0)
stop, tgt, rr = D.levels(f3, 100.0, "long")
ok(stop == 96.0, f"stop clamped to at most 2 ATR (got {stop})")
f4 = dict(f, high20=99.0, high60=99.5)
stop, tgt, rr = D.levels(f4, 100.0, "long")
ok(tgt == 106.0 and rr == 2.0, f"no overhead level -> 2R target (got {tgt}, {rr})")
f5 = dict(f, high20=101.0, high60=112.0)
stop, tgt, rr = D.levels(f5, 100.0, "long")
ok(tgt == 109.0 and rr == 3.0, f"level under 1R ignored, far level capped at 3R (got {tgt}, {rr})")
ok(D.tier_for(8, 0.9) == "shadow" and D.tier_for(8, 1.0) == "A", "R:R below 1.0 can never trade")
stop, tgt, rr = D.levels(f, 100.0, "short")
ok(stop == 103.0 and tgt == 96.0, f"short mirror stop 103 target 96 (got {stop},{tgt})")
ok(D.size(100000, 0.005, 100.0, 97.0) == 166, "size: 500 risk / 3 per share = 166 shares")
ok(D.size(100000, 0.005, 100.0, 99.9) == 200, "size capped at 20% notional")
ok(D.r_multiple("short", 100, 103, 94) == 2.0, "short R multiple")
ok(D.tier_for(7) == "A" and D.tier_for(6) == "B" and D.tier_for(4) == "shadow", "tiers")

# ---------------- timestamp parsing (Alpaca sends 8-9 fractional digits) ----------------
print("timestamps")
for ts in ("2026-10-16T19:14:53.39245605+00:00", "2026-10-16T19:14:53.392456789Z", "2026-10-16T19:14:53Z",
           "2026-10-16T19:14:53.5Z", "2026-10-16T19:14:53.123Z"):
    ok(sync.et_date(ts) == "2026-10-16", f"parses {ts}")
ok(sync.et_date("2026-10-17T02:30:00.123456789Z") == "2026-10-16", "converts to ET date (late UTC -> same ET day)")

# ---------------- briefing ----------------
print("briefing")
reset_journal()
fb = FakeBroker()
D.set_broker(fb)
r = score.run("briefing")
ok(len(r["top"]) == 5, "briefing returns top 5")
ok(D.read_jsonl(D.CANDIDATES) == [], "briefing logs nothing")
ok(any("No regime file" in w for w in r["warnings"]), "warns when regime file missing")
print(score.summary(r))

# ---------------- decision with regime ----------------
print("decision")
regime = {"themes": {"energy": {"long": 2, "short": 0, "why": "test oil"}, "bonds": {"short": 2, "long": 0, "why": "test yields"},
                     "metals": {"short": 2, "why": "test dollar"}}}
(TMP / "reports" / f"regime_{TODAY}.json").write_text(json.dumps(regime))
r = score.run("decision")
print(score.summary(r))
plan = r["plan"]
ok(len(plan) >= 1, f"decision produced {len(plan)} planned orders")
ok(len(plan) <= 5, "never more than 5 positions")
themes = [c["theme"] for c in plan]
ok(all(themes.count(t) <= 2 for t in themes), "max 2 per theme")
ok(sum(c["risk_dollars"] for c in plan) <= 2500, "total risk <= 2.5%")
ok(all(c["score"] >= 5 for c in plan), "only A/B scores planned")
ok(all(c["rr"] >= 1.0 for c in plan), "every planned trade has R:R >= 1.0")
ok(not any(c["symbol"] == "GDX" and c["direction"] == "short" for c in plan), "GDX short not planned (not easy to borrow)")
ok(any(c["symbol"] == "GDX" and "shortable" in (c["block_reason"] or "") for c in r["blocked"]) or
   not any(c["symbol"] == "GDX" for c in r["blocked"] + plan), "GDX short blocked by the borrow check when it qualifies")
start = json.loads(D.START_FILE.read_text())
ok(start["equity"] == 100000.0 and "spy_close" in start, "start_date.txt initialised")
logged = D.read_jsonl(D.CANDIDATES)
ok(len(logged) == len(D.UNIVERSE), "one candidate row per symbol logged")
energy_blocked = [c for c in logged if c["action"] == "blocked" and "theme" in (c["block_reason"] or "")]
print("   blocked by theme:", [c["symbol"] for c in energy_blocked])

# ---------------- random control group ----------------
print("controls")
ctrl = D.read_jsonl(D.CONTROL)
ok(len(ctrl) == 5 and all(c["date"] == str(TODAY) and c["status"] == "open" for c in ctrl), "decision run drew 5 open controls")
ok(len({c["symbol"] for c in ctrl}) == 5, "controls are 5 different symbols")
ok(all(c["rr"] >= 1.0 for c in ctrl), "controls obey the R:R >= 1.0 gate")
ok(all(not (c["symbol"] == "GDX" and c["direction"] == "short") for c in ctrl), "controls obey the shortable gate")
ok(all(c["direction"] in ("long", "short") for c in ctrl), "controls have a direction")
first_draw = [(c["symbol"], c["direction"]) for c in ctrl]
try:
    score.run("decision")
    ok(False, "a second DECISION run must be refused")
except SystemExit as e:
    ok("DECISION ALREADY RAN" in str(e), "a second DECISION run the same day is refused without --force")
rr_again = score.run("decision", force=True)
ok(len(D.read_jsonl(D.CONTROL)) == 5 and rr_again["controls_drawn"] == 0, "rerunning decision does not double-draw controls")
D.write_jsonl(D.CONTROL, [])
control_rows, _ = control.draw({s: {d: c for d, c in [("long", {"rr": 2.0, "entry": 10, "stop": 9, "target": 12}),
                                                         ("short", {"rr": 2.0, "entry": 10, "stop": 11, "target": 8})]}
                                for s in ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG")}, set(), fb, str(TODAY), True)
again = []
D.write_jsonl(D.CONTROL, [])
again, _ = control.draw({s: {d: c for d, c in [("long", {"rr": 2.0, "entry": 10, "stop": 9, "target": 12}),
                                                  ("short", {"rr": 2.0, "entry": 10, "stop": 11, "target": 8})]}
                         for s in ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG")}, set(), fb, str(TODAY), True)
ok([(r["symbol"], r["direction"]) for r in control_rows] == [(r["symbol"], r["direction"]) for r in again],
   "the draw is deterministic for a given date (cannot be cherry-picked)")
ok(len(control_rows) == 5, "draw caps at 5 per day")
bl, _ = control.draw({s: {d: {"rr": 2.0, "entry": 10, "stop": 9, "target": 12} for d in ("long", "short")}
                      for s in ("AAA", "BBB")}, {"AAA", "BBB"}, fb, "2099-01-01", True)
ok(bl == [], "earnings blackout symbols are never drawn")
lo, _ = control.draw({"AAA": {d: {"rr": 0.5, "entry": 10, "stop": 9, "target": 10.5} for d in ("long", "short")}},
                     set(), fb, "2099-01-02", True)
ok(lo == [], "R:R below 1.0 is never drawn")
D.write_jsonl(D.CONTROL, ctrl)    # restore the real draw for the rest of the run

# ---------------- place ----------------
print("place")
c0 = plan[0]
try:
    place.place(c0["symbol"], "short", "x", "y", 0.4)
    ok(False, "should refuse short thesis")
except SystemExit as e:
    ok("REFUSED" in str(e), "refuses empty thesis")
try:
    place.place("ZZZZ", "a real thesis here", "a real invalidation", "a real dissent line", 0.4)
except SystemExit as e:
    ok("not in today's plan" in str(e), "refuses symbol not in plan")
base0 = 1 / (1 + c0["rr"])
for bad in (round(base0 + 0.20, 2), round(max(0.06, base0 - 0.20), 2), 0.5 if abs(0.5 - base0) > 0.15 else 0.9):
    try:
        place.place(c0["symbol"], "Strong trend and regime support", "Daily close below the stop level",
                    "Fails if the sector reverses on macro news", bad, earnings_checked=True)
        ok(False, f"should refuse p_target {bad}")
    except SystemExit as e:
        ok("outside the allowed range" in str(e), f"refuses p_target {bad} (base rate {base0:.2f}, allowed +/-0.15)")
for c in plan:
    place.place(c["symbol"], "Strong trend and regime support", "Daily close below the stop level",
                "Fails if the sector reverses on macro news", round(1 / (1 + c["rr"]) + 0.05, 2), earnings_checked=True)
trades = D.read_jsonl(D.TRADES)
ok(len(trades) == len(plan) and all(t["status"] == "pending" for t in trades), "all placed and logged pending")
try:
    place.place(c0["symbol"], "Strong trend and regime support", "Daily close below the stop level",
                "Fails if the sector reverses on macro news", round(base0 + 0.05, 2), earnings_checked=True)
except SystemExit as e:
    ok("already" in str(e), "refuses duplicate placement")
ok(all("p_base" in t and abs(t["p_base"] - 1 / (1 + c["rr"])) < 0.001 for t, c in zip(D.read_jsonl(D.TRADES), plan)), "p_base logged on every trade")

# rerun decision: symbols now busy, should not re-plan them
r2 = score.run("decision", force=True)
ok(not set(c["symbol"] for c in r2["plan"]) & set(t["symbol"] for t in trades), "decision does not re-plan busy symbols")
D.write_jsonl(D.CANDIDATES, logged)   # restore single decision log for review checks

# ---------------- sync ----------------
print("sync")
t0, t1 = trades[0], trades[1] if len(trades) > 1 else None
fb.fill_entry(t0["order_id"], t0["planned_entry"])
sync.run()
t = D.read_jsonl(D.TRADES)[0]
ok(t["status"] == "open" and t["entry_fill"] == t0["planned_entry"], "entry fill recorded")
fb.fill_leg(t0["order_id"], "limit", t0["target"])
sync.run()
t = D.read_jsonl(D.TRADES)[0]
ok(t["status"] == "closed" and t["exit_reason"] == "target", "target exit recorded")
exp_R = D.r_multiple(t["direction"], t["entry_fill"], t["stop"], t["target"])
ok(abs(t["result_R"] - exp_R) < 1e-9 and t["result_R"] > 0, f"result_R = {t['result_R']}")
if t1:
    sync.run(close_mode=True)
    t = D.read_jsonl(D.TRADES)[1]
    ok(t["status"] == "unfilled", "close mode cancels unfilled entries")

# time stop
print("time stop")
rows = D.read_jsonl(D.TRADES)
fb.n += 1
oid = f"ord{fb.n}"
fb.orders[oid] = {"id": oid, "symbol": "XLF", "status": "filled", "filled_avg_price": "50", "filled_at": "x",
                  "legs": [{"id": oid + "tp", "type": "limit", "status": "new"}, {"id": oid + "sl", "type": "stop", "status": "new"}]}
fb.pos["XLF"] = {"symbol": "XLF", "current_price": "51"}
old = (TODAY - dt.timedelta(days=20)).isoformat()
rows.append({"trade_id": "old-XLF", "order_id": oid, "rules_version": "v1.0", "date_open": old, "date_fill": old, "symbol": "XLF",
             "direction": "long", "tier": "B", "score": 5, "entry_fill": 50.0, "stop": 49.0, "target": 53.0, "shares": 10,
             "risk_dollars": 10, "p_target": 0.3, "thesis": "t" * 12, "invalidation": "i" * 12, "dissent": "d" * 12, "status": "open"})
D.write_jsonl(D.TRADES, rows)
sync.run(close_mode=True)
t = [x for x in D.read_jsonl(D.TRADES) if x["trade_id"] == "old-XLF"][0]
ok(t["status"] == "closed" and t["exit_reason"] == "time" and t["result_R"] == 1.0, "10-day time stop closes at market (+1R)")
ok(oid + "tp" in fb.cancelled and oid + "sl" in fb.cancelled, "bracket legs cancelled before closing")

# control resolution
print("control resolution")
i0 = len(fb.days) - 25
d0 = fb.days[i0].isoformat()
e0 = fb.bars["SPY"][i0]["c"]
D.write_jsonl(D.CONTROL, [
    {"date": d0, "rules_version": "v1.0", "symbol": "SPY", "direction": "long", "entry": e0, "stop": e0 * 0.5, "target": e0 * 1.005,
     "rr": 2.0, "status": "open"},
    {"date": d0, "rules_version": "v1.0", "symbol": "QQQ", "direction": "long", "entry": fb.bars["QQQ"][i0]["c"],
     "stop": 1.0, "target": 9999.0, "rr": 2.0, "status": "open"},
    {"date": TODAY.isoformat(), "rules_version": "v1.0", "symbol": "XLE", "direction": "long", "entry": 100, "stop": 90,
     "target": 120, "rr": 2.0, "status": "open"}])
done = control.resolve(fb, TODAY.isoformat())
rows_c = D.read_jsonl(D.CONTROL)
ok(len(done) == 2 and rows_c[0]["exit_reason"] == "target" and rows_c[0]["result_R"] > 0, "control hits its target and resolves positive")
ok(rows_c[1]["exit_reason"] == "time" and rows_c[1]["status"] == "resolved", "control with no touch exits at the 10-day time stop")
ok(rows_c[2]["status"] == "open", "a control drawn today is not resolved the same day")
gap_df = D.bars_to_df([{"t": f"2026-01-0{i}T05:00:00Z", "o": o, "h": h, "l": l, "c": c, "v": 1} for i, (o, h, l, c) in
                       enumerate([(100, 101, 99, 100), (90, 92, 88, 91)], start=5)])
out = D.simulate_path(gap_df, "long", 100.0, 95.0, 110.0, dt.date(2026, 1, 5), horizon=1)
ok(out == (-2.0, "stop"), f"gap through the stop fills at the open (worse than -1R): {out}")
tie = D.bars_to_df([{"t": "2026-01-06T05:00:00Z", "o": 100, "h": 111, "l": 94, "c": 100, "v": 1}])
ok(D.simulate_path(tie, "long", 100.0, 95.0, 110.0, dt.date(2026, 1, 5), horizon=1) == (-1.0, "stop"), "same-day target+stop counts as a stop")

# ---------------- quota ----------------
print("quota")
reset_journal()
(TMP / "start_date.txt").write_text(json.dumps({"date": (TODAY - dt.timedelta(days=14)).isoformat(), "spy_close": 100, "equity": 100000}))
fq = FakeBroker(flat=True, seed=7)
D.set_broker(fq)
real_score = D.score
D.score = lambda f, d, pts, rr, is_spy=False: (4, {"trend": 1, "rs": 1, "regime": pts, "rr": 0, "momentum": 0, "volume": 0})
rq = score.run("decision")
D.score = real_score
print(score.summary(rq))
ok(rq["trading_days_since_last_AB"] >= 5, "idle >= 5 trading days detected")
ok(len(rq["plan"]) == 1 and rq["plan"][0]["quota"] and rq["plan"][0]["tier"] == "B", "quota trade planned as B when nothing scores 5+")
ok(rq["plan"][0]["risk_pct"] == 0.0025 and rq["plan"][0]["rr"] >= 1.0, "quota sized at 0.25% and R:R >= 1")
(TMP / "start_date.txt").write_text(json.dumps({"date": (TODAY - dt.timedelta(days=2)).isoformat(), "spy_close": 100, "equity": 100000}))
reset_journal()
D.score = lambda f, d, pts, rr, is_spy=False: (4, {"trend": 1, "rs": 1, "regime": pts, "rr": 0, "momentum": 0, "volume": 0})
rq2 = score.run("decision")
D.score = real_score
ok(not rq2["plan"], "no quota trade before 5 idle trading days")

# daily halt
print("daily halt")
reset_journal()
fh = FakeBroker()
fh.equity = 98000.0
D.set_broker(fh)
rh = score.run("decision")
ok(rh["halted"] and not rh["plan"], "daily halt blocks entries at -2%")

# ---------------- review ----------------
print("review")
reset_journal()
fr = FakeBroker(n_bars=120)
D.set_broker(fr)
(TMP / "start_date.txt").write_text(json.dumps({"date": fr.days[-40].isoformat(), "spy_close": fr.bars["SPY"][-40]["c"], "equity": 100000}))
cand_rows, trade_rows = [], []
for i, day in enumerate(fr.days[-35:-12]):
    for s, act, score_ in (("XLE", "planned", 7), ("TLT", "shadow", 3), ("QQQ", "blocked", 6)):
        df = D.bars_to_df(fr.bars[s])
        px = float(df[df["date"] == day]["close"].iloc[0])
        d = "short" if s == "TLT" else "long"
        stop, tgt = (px * 0.98, px * 1.04) if d == "long" else (px * 1.02, px * 0.96)
        cand_rows.append({"date": day.isoformat(), "run": "decision", "rules_version": "v1.0", "symbol": s, "direction": d,
                          "score": score_, "entry": px, "stop": stop, "target": tgt, "rr": 2.0, "tier": "A", "action": act,
                          "breakdown": {"trend": 2, "rs": 1, "regime": 1 + (i % 2), "rr": 2, "momentum": 1, "volume": 1}})
for i in range(25):
    win = i % 3 == 0
    trade_rows.append({"trade_id": f"t{i}", "order_id": f"o{i}", "rules_version": "v1.0", "date_open": fr.days[-35 + i // 2].isoformat(),
                       "symbol": "XLE", "direction": "long", "tier": "A" if i % 2 else "B", "score": 7 if i % 2 else 5,
                       "entry_fill": 100, "stop": 98, "target": 104, "shares": 100, "risk_dollars": 200 if i % 2 else 200,
                       "p_target": 0.45 if win else 0.25, "p_base": 0.333, "thesis": "x" * 20, "invalidation": "y" * 20, "dissent": "z" * 20,
                       "status": "closed", "date_close": fr.days[-30 + i // 2].isoformat(), "exit_reason": "target" if win else "stop",
                       "result_R": 2.0 if win else -1.0, "pnl": 400 if win else -200})
D.write_jsonl(D.CANDIDATES, cand_rows)
D.write_jsonl(D.TRADES, trade_rows)
rng_ = __import__("random").Random(3)
D.write_jsonl(D.CONTROL, [{"date": fr.days[-30].isoformat(), "rules_version": "v1.0", "symbol": "SPY", "direction": "long",
                           "entry": 100, "stop": 98, "target": 104, "rr": 2.0, "status": "resolved",
                           "result_R": rng_.choice([-1.0, -1.0, 2.0]), "exit_reason": "x"} for _ in range(80)])
rep = review.run()
print(rep)
ok("Win rate 36.0%" in rep, "win rate computed (9/25)")
ok("+0.08R" in rep, "expectancy computed ((9*2-16)/25 = +0.08R)")
ok("5b. Random baseline" in rep and "Random controls (all resolved): 80" in rep, "random baseline section reports controls")
ok("PRIMARY TEST" in rep and "fortnights" in rep, "primary test section reports its fortnight count")
ok("Brier score" in rep and "Brier skill" in rep and "neutral base rate" in rep, "calibration reported with the base-rate anchor check")
ok("5c. Does the score predict forward R" in rep and "matured candidate-trades" in rep, "score study section reports matured candidates")
ok("Factor regime:" in rep and "Score 7:" in rep and "Significance test withheld" in rep, "score buckets and factor lines shown; significance withheld on small samples")
ok("Verdict withheld" in rep and "12+ fortnights" in rep, "no verdict until 12+ fortnights of data (5 weeks here)")
ok("planned: " in rep and "resolved" in rep, "shadow book simulated")
ok("Sample too small" not in rep, "no small-sample warning at 25 trades")
ok("planned but never placed" in rep, "flags planned-but-not-placed candidates")
ok(D.WEEKLY.read_text().count("## Weekly review") == 1, "appended to weekly.md")
D.write_jsonl(D.CONTROL, D.read_jsonl(D.CONTROL)[:10])
rep_off = review.run(offline=True)
ok("Skipped" in rep_off, "offline mode works without Alpaca")
ok(D.WEEKLY.read_text().count("## Weekly review") == 1, "re-running the review the same day replaces its section (no duplicate)")
ok("Verdict withheld" in rep_off, "verdict withheld when there are too few controls")
rr_ = __import__("random").Random(11)
pos = [{"date": f"d{d}", "score": sc, "R": (sc - 5) * 0.3 + rr_.gauss(0, 0.5)} for d in range(30) for sc in range(3, 9)]
neg = [{"date": f"d{d}", "score": sc, "R": rr_.gauss(0, 1.0)} for d in range(30) for sc in range(3, 9)]
r1, p1 = review.within_day_perm_p(pos, 500)
r2, p2 = review.within_day_perm_p(neg, 500)
ok(r1 > 0.5 and p1 < 0.01 and p1 > 0, f"permutation test detects a real score->R relationship (r={r1:.2f}, p={p1:.3f}, never 0)")
ok(abs(r2) < 0.3 and p2 > 0.01, f"permutation test stays quiet on noise (r={r2:.2f}, p={p2:.3f})")
young = [{"date": str(TODAY - dt.timedelta(days=3)), "symbol": "SPY", "direction": "long", "score": 8, "entry": 100.0,
          "stop": 1.0, "target": 100.01, "breakdown": {}}]
spy_df = D.bars_to_df(fr.bars["SPY"])
ok(review.score_study(young, {"SPY": (spy_df, spy_df)}) == [], "candidates younger than 10 trading days are excluded (no early-exit bias)")
rr3 = __import__("random").Random(5)
dd0 = dt.date(2026, 1, 5)
def rows_(edge, mkt, lean):
    out_a, out_b = [], []
    for i in range(200):
        day = (dd0 + dt.timedelta(days=i)).isoformat()
        m = mkt[i // 14]
        sa = 1 if rr3.random() < lean else -1
        out_a.append((day, "long" if sa > 0 else "short", sa * m + edge + rr3.gauss(0, 1)))
        for _ in range(2):
            sb = rr3.choice([1, -1])
            out_b.append((day, "long" if sb > 0 else "short", sb * m + rr3.gauss(0, 1)))
    return out_a, out_b
mkt = [rr3.gauss(0, 2) for _ in range(20)]
a1, b1 = rows_(0.6, mkt, 1.0)
d1, pp1, nb1 = control.compare(a1, b1, n_perm=2000)
ok(d1 > 0.3 and pp1 < 0.01 and pp1 > 0 and nb1 >= 12, f"the primary test detects a real edge (diff {d1:+.2f}, p={pp1:.4f}, {nb1} fortnights, p never 0)")
a2, b2 = rows_(0.0, [abs(x) + 2 for x in mkt], 1.0)
d2, pp2, _ = control.compare(a2, b2, n_perm=2000)
ok(pp2 > 0.05, f"a long-only plan in a rising market is NOT called skill (direction-matched; p={pp2:.3f})")
one = [("2026-01-06", "long", 1.0)] * 30
other = [("2026-03-02", "long", -1.0)] * 60
_, _, nbx = control.compare(one, other, n_perm=200)
ok(nbx == 0, "plans and controls from different fortnights are never compared with each other")
ok("no evidence" in control.verdict(0.2, 0.5) and "evidence of skill" in control.verdict(0.3, 0.01)
   and "hurting" in control.verdict(-0.3, 0.01) and "no evidence" in control.verdict(0.3, 0.06), "verdict wording (two-sided)")


# =====================================================================================================
# Hardening tests (one block per review finding)
# =====================================================================================================
import subprocess   # noqa: E402

PLAN_TEXT = ("Strong trend and regime support", "Daily close below the stop level", "Fails if the sector reverses on macro news")


def fresh(**kw):
    reset_journal()
    for f in (TMP / "journal").glob("corrupt_lines.txt"):
        f.unlink()
    (TMP / "start_date.txt").write_text("PENDING\n")
    b = FakeBroker(**kw)
    D.set_broker(b)
    return b


def write_regime(obj):
    (TMP / "reports" / f"regime_{TODAY}.json").write_text(json.dumps(obj))


def expect_exit(fn, needle, msg):
    try:
        fn()
    except SystemExit as e:
        ok(needle in str(e), f"{msg} ({str(e)[:70]!r})")
        return str(e)
    ok(False, msg + " (no refusal)")


print("journal integrity")
fresh()
D.append_jsonl(D.TRADES, [{"a": 1}])
with open(D.TRADES, "a") as fh:
    fh.write('{"torn": tr')            # crash mid-write: no newline
D.append_jsonl(D.TRADES, [{"b": 2}])
rows = D.read_jsonl(D.TRADES)
ok({"b": 2} in rows and {"a": 1} in rows, "a row appended after a torn write stays intact (not glued to the torn line)")
ok(D.CORRUPT_FILE.exists() and "torn" in D.CORRUPT_FILE.read_text(), "the torn line is copied to corrupt_lines.txt, not silently dropped")
D.write_jsonl(D.TRADES, rows)
D.read_jsonl(D.TRADES)
ok(D.CORRUPT_FILE.read_text().count("torn") == 1, "a corrupted line is quarantined once, not on every read")
with D.journal_lock():
    with D.journal_lock():
        D.append_jsonl(D.TRADES, [{"c": 3}])
ok({"c": 3} in D.read_jsonl(D.TRADES), "the journal lock is re-entrant inside one run")
with D.journal_lock():
    code = ("import os,sys; os.environ['DESK_ROOT']=sys.argv[1]; sys.path.insert(0, sys.argv[2]); import desk as D\n"
            "try:\n    D.journal_lock(timeout_s=0.5).__enter__(); print('GOT')\nexcept SystemExit as e: print('BUSY', e)")
    out = subprocess.run([sys.executable, "-c", code, str(TMP), str(Path(__file__).resolve().parent.parent / "scripts")],
                         capture_output=True, text=True, timeout=30).stdout
ok(out.startswith("BUSY"), "a second process can't write while a run holds the journal lock")

print("rules version")
(TMP / "rules_version.txt").write_text("v1.1\n")
expect_exit(D.rules_version, "RULES VERSION MISMATCH", "editing rules_version.txt alone is refused (can't reset the scorecard)")
(TMP / "rules_version.txt").write_text("v1.0\n")

print("regime validation")
errs, warns = D.validate_regime({"date": "x", "catalysts": [], "catcalysts": [], "themes": {"energy": {"long": 2, "short": 0, "why": "w"}}})
ok(not errs and any("catcalysts" in w for w in warns), "real-world files (date, catalysts, a typo'd extra key) pass with a warning")
for bad, needle in (({"themes": {"Energy": {"long": 2}}}, "unknown theme"),
                    ({"themes": {"energy": {"Long": 2}}}, "unknown key"),
                    ({"themes": {"energy": {"long": "two"}}}, "whole number"),
                    ({"themes": {"energy": {"long": True}}}, "whole number"),
                    ({"themes": {"energy": {"long": 3}}}, "whole number"),
                    ({"symbols": {"ZZZ": {"long": 2}}}, "unknown symbol"),
                    ({"exclude": {"XLE": "earnings 2026-10-29 (nasdaq)"}}, "only single stocks"),
                    ({"exclude": {"META": "x"}}, "earnings date"), ({"exclude": {"META": "earnings soon, nasdaq.com"}}, "YYYY-MM-DD")):
    e2, _ = D.validate_regime(bad)
    ok(any(needle in e for e in e2), f"regime file rejected: {needle}")
fresh()
write_regime({"themes": {"energy": {"long": "two"}}})
expect_exit(lambda: score.run("decision"), "REGIME FILE INVALID", "score.py refuses an invalid regime file")
ok(D.read_jsonl(D.CANDIDATES) == [], "nothing is logged when the regime file is invalid")

print("market calendar")
fb2 = fresh()
fb2.is_open = False
expect_exit(lambda: score.run("decision"), "MARKET NOT OPEN", "DECISION refuses to run outside market hours")
fb2.is_open, fb2.holidays = True, {TODAY.isoformat()}
expect_exit(lambda: score.run("decision"), "MARKET CLOSED TODAY", "DECISION refuses to run on a market holiday")
ok(D.read_jsonl(D.CANDIDATES) == [] and D.read_jsonl(D.CONTROL) == [], "nothing logged on a holiday")
rb = score.run("briefing")
ok(any("not a trading day" in w for w in rb["warnings"]), "a holiday briefing still runs, with a warning")
fb2.holidays = set()
fb2.calendar_fails = True
expect_exit(lambda: score.run("decision"), "UNAVAILABLE", "no calendar: DECISION refuses instead of guessing")
fb2.calendar_fails = False

print("stale data")
fb3 = fresh()
for row in range(3):
    fb3.bars["XLU"].pop()                 # XLU's newest bars are missing
r = score.run("briefing")
ok(any(w.startswith("XLU") and "Stale" in w for w in r["warnings"]) and all(c["symbol"] != "XLU" for c in r["all"]),
   "a symbol whose newest bar is old is skipped with a warning")

print("exclusions")
fb4 = fresh()
expect_exit(lambda: score.run("decision", exclude=("XLE",)), "only single stocks", "--exclude on an ETF is refused (no vetoes)")
SOON = D.trading_days_ahead(fb4, str(TODAY), 2)
write_regime({"themes": {"tech": {"long": 2, "short": 0, "why": "t"}}, "exclude": {"NVDA": f"earnings {SOON} (nvidia.com 10/28)"}})
r_far = None
write_regime({"exclude": {"NVDA": "earnings 2027-05-20 (nvidia.com)"}})
expect_exit(lambda: score.run("decision"), "outside the 10-trading-day window" if False else "no earnings date between",
            "an exclusion dated outside the 10-trading-day window is refused (no vetoes on stocks either)")
write_regime({"themes": {"tech": {"long": 2, "short": 0, "why": "t"}}, "exclude": {"NVDA": f"earnings {SOON} (nvidia.com 10/28)"}})
r = score.run("decision")
nv = [c for c in D.read_jsonl(D.CANDIDATES) if c["symbol"] == "NVDA"]
ok(nv and nv[0]["excluded"], "a stock excluded in the regime file is logged as excluded")
ok("NVDA" in r["exclusions"] and "nvidia.com" in r["exclusions"]["NVDA"], "the exclusion reason is recorded")
ok(all(c.get("regime_sha") for c in D.read_jsonl(D.CANDIDATES)), "every candidate row records which regime file scored it")

print("earnings check (per stock)")
fb5 = fresh()
os.environ["FINNHUB_API_KEY"] = "tok_SECRET_123"
calls = []


def fake_finnhub(session, sym, start, end, key):
    calls.append(sym)
    if sym == "TSLA":
        raise ConnectionError(f"https://finnhub.io/api?token={key} timed out")
    if sym == "META":
        return [{"symbol": "META", "date": start}]
    return [{"symbol": sym, "date": (dt.date.fromisoformat(start) + dt.timedelta(days=60)).isoformat()}]


real_fh = score._finnhub_get
score._finnhub_get = fake_finnhub
bl, ver, note = score.earnings_blackout(fb5, TODAY.isoformat())
score._finnhub_get = real_fh
ok(sorted(calls) == sorted(D.STOCKS), "every stock gets its own earnings lookup")
ok(bl == {"META"}, "a stock reporting inside the window is blacked out")
ok("TSLA" not in ver and "AAPL" in ver, "a failed lookup leaves only that stock unverified")
ok("tok_SECRET_123" not in note and "TSLA" in note, "the note names the failure without ever printing the API token")
del os.environ["FINNHUB_API_KEY"]
bl, ver, note = score.earnings_blackout(fb5, TODAY.isoformat())
ok(not ver and "no FINNHUB_API_KEY" in note, "no key: everything unverified and the warning says why")

print("controls")
fb6 = fresh()
both = {s: {d: {"rr": 2.0, "entry": 10, "stop": 9 if d == "long" else 11, "target": 12 if d == "long" else 8}
            for d in ("long", "short")} for s in ("AAPL", "MSFT", "XLE", "XLF", "SPY", "QQQ", "IWM", "TLT")}
rows_c, _ = control.draw(both, set(), fb6, "2099-02-01", verified=set())
ok(rows_c and all(r["symbol"] not in D.STOCKS for r in rows_c), "stocks whose earnings weren't verified are never drawn as controls")
ok(all("py" in r for r in rows_c), "the Python version is logged with each control (reproducibility)")


class AssetDown(FakeBroker):
    def asset(self, s):
        raise BR.BrokerError("asset lookup down")


ad = AssetDown()
D.write_jsonl(D.CONTROL, [])
try:
    control.draw(both, set(), ad, "2099-02-03", verified=set(D.STOCKS))
    drew_anyway = True
except BR.BrokerError:
    drew_anyway = False
ok(not drew_anyway and D.read_jsonl(D.CONTROL) == [], "a failed shortable lookup aborts the draw instead of logging a long-only day")

print("split-safe simulation")
days_ = [dt.date(2026, 3, 2) + dt.timedelta(days=i) for i in range(14)]
raw_rows = [{"t": f"{d}T05:00:00Z", "o": 1000, "h": 1005, "l": 995, "c": 1000, "v": 1} for d in days_[:4]] + \
           [{"t": f"{d}T05:00:00Z", "o": 100, "h": 100.5, "l": 99.5, "c": 100, "v": 1} for d in days_[4:]]
split_rows = [dict(r, o=r["o"] / 10 if i < 4 else r["o"], h=r["h"] / 10 if i < 4 else r["h"],
                   l=r["l"] / 10 if i < 4 else r["l"], c=r["c"] / 10 if i < 4 else r["c"]) for i, r in enumerate(raw_rows)]
fr_s = (D.bars_to_df(split_rows), D.bars_to_df(raw_rows))
out = D.simulate_logged(fr_s, "long", 1000.0, 950.0, 1100.0, days_[1])
ok(out is not None and out[1] == "time" and abs(out[0]) < 0.01, f"a 10:1 split after the entry doesn't fake a -18R loss ({out})")
div_split = [{"t": f"{d}T05:00:00Z", "o": 100, "h": 101, "l": 99.5, "c": 100, "v": 1} for d in days_]
div_all = [dict(r, o=r["o"] - 0.35 if i < 3 else r["o"], h=r["h"] - 0.35 if i < 3 else r["h"],
                l=r["l"] - 0.35 if i < 3 else r["l"], c=r["c"] - 0.35 if i < 3 else r["c"]) for i, r in enumerate(div_split)]
out = D.simulate_logged((D.bars_to_df(div_split), D.bars_to_df(div_split)), "long", 100.0, 99.0, 101.0, days_[1])
ok(out == (1.0, "target"), f"dividends no longer shift history under the logged levels ({out})")

print("place: order files")
fb7 = fresh()
write_regime({"themes": {"energy": {"long": 2, "short": 0, "why": "e"}}})
plan7 = score.run("decision")["plan"]
c7 = plan7[0]
p7 = round(1 / (1 + c7["rr"]), 2)
of = TMP / "reports" / f"order_{c7['symbol']}_{TODAY}.json"
of.write_text(json.dumps({"symbol": c7["symbol"], "p_target": p7, "thesis": "Brent at $105.92 and the regime file at 2",
                          "invalidation": "Daily close below the stop at $90.10", "dissent": "Fails if `rm -rf` $(whoami) news hits",
                          "earnings_checked": True}))
place.main(["--input", str(of)])
t7 = [t for t in D.read_jsonl(D.TRADES) if t["symbol"] == c7["symbol"]][0]
ok("$105.92" in t7["thesis"] and "$(whoami)" in t7["dissent"], "dollar signs and shell syntax in an order file arrive untouched (never run)")
ok(t7["text_source"] == "file" and t7["status"] == "pending", "trade logged from the file as pending")
outside = TMP / "order_X.json"
outside.write_text("{}")
expect_exit(lambda: place.main(["--input", str(outside)]), "must be reports/order_", "order files outside reports/ are refused")
bad = TMP / "reports" / f"order_BAD_{TODAY}.json"
bad.write_text(json.dumps({"symbol": "XLE", "p_target": 0.3, "thesis": "x" * 20, "invalidation": "y" * 20,
                           "dissent": "z" * 20, "shell": "rm -rf /"}))
expect_exit(lambda: place.main(["--input", str(bad)]), "unknown keys", "unknown keys in an order file are refused")
bad.write_text(json.dumps({"symbol": "XLE", "p_target": True, "thesis": "x" * 20, "invalidation": "y" * 20, "dissent": "z" * 20}))
expect_exit(lambda: place.main(["--input", str(bad)]), "p_target must be a number", "p_target true/false is refused")
bad.write_text(json.dumps({"symbol": "../../X", "p_target": 0.3, "thesis": "x" * 20, "invalidation": "y" * 20, "dissent": "z" * 20}))
expect_exit(lambda: place.main(["--input", str(bad)]), "1-5 capital letters", "a malformed symbol is refused")
bad.write_text(json.dumps({"symbol": "XLE", "p_target": 0.3, "thesis": "x" * 700, "invalidation": "y" * 20, "dissent": "z" * 20}))
expect_exit(lambda: place.main(["--input", str(bad)]), "keep it under", "an oversized thesis is refused")

print("place: failures are never reported as PLACED")
c8 = plan7[1] if len(plan7) > 1 else None
if c8:
    fb7.submit_mode = "reject"
    expect_exit(lambda: place.place(c8["symbol"], *PLAN_TEXT, round(1 / (1 + c8["rr"]), 2), True), "ORDER REJECTED",
                "an order Alpaca rejects is reported as rejected")
    t8 = [t for t in D.read_jsonl(D.TRADES) if t["symbol"] == c8["symbol"]][0]
    ok(t8["status"] == "rejected", "the rejected order is in the journal as rejected (not pending)")
c9 = plan7[2] if len(plan7) > 2 else None
if c9:
    fb7.submit_mode = "timeout_after_accept"
    place.place(c9["symbol"], *PLAN_TEXT, round(1 / (1 + c9["rr"]), 2), True)
    t9 = [t for t in D.read_jsonl(D.TRADES) if t["symbol"] == c9["symbol"]][0]
    ok(t9["status"] == "pending" and t9["order_id"], "a timeout after Alpaca accepted the order is recovered by client id")
c10 = plan7[3] if len(plan7) > 3 else None
if c10:
    fb7.submit_mode = "error"
    expect_exit(lambda: place.place(c10["symbol"], *PLAN_TEXT, round(1 / (1 + c10["rr"]), 2), True), "ORDER NOT PLACED",
                "a submit that never reached Alpaca says NOT PLACED")
    t10 = [t for t in D.read_jsonl(D.TRADES) if t["symbol"] == c10["symbol"]][0]
    ok(t10["status"] == "failed_submit", "and is journaled as failed_submit")
c10b = plan7[4] if len(plan7) > 4 else None
if c10b:
    fb7.submit_mode = "error500"
    expect_exit(lambda: place.place(c10b["symbol"], *PLAN_TEXT, round(1 / (1 + c10b["rr"]), 2), True), "ORDER STATUS UNKNOWN",
                "a 5xx/timeout with no order found is UNKNOWN, not 'not placed' (Alpaca may still have it)")
    t10b = [t for t in D.read_jsonl(D.TRADES) if t["symbol"] == c10b["symbol"]][0]
    ok(t10b["status"] == "submitting", "the row stays 'submitting' for sync.py to settle")
fb7.submit_mode = None
fb7.is_open = False
c11 = plan7[-1]
if not any(t["symbol"] == c11["symbol"] for t in D.read_jsonl(D.TRADES)):
    expect_exit(lambda: place.place(c11["symbol"], *PLAN_TEXT, round(1 / (1 + c11["rr"]), 2), True), "market is not open",
                "place.py refuses to send orders while the market is closed")
fb7.is_open = True

print("place: crash between logging and submitting")
fb12 = fresh()
plan12 = score.run("decision")["plan"]
c12 = plan12[0]
tid = f"{TODAY}-{c12['symbol']}-{c12['direction']}"
D.append_jsonl(D.TRADES, [{"trade_id": tid, "order_id": None, "symbol": c12["symbol"], "direction": c12["direction"],
                           "status": "submitting", "shares": c12["shares"], "stop": c12["stop"], "target": c12["target"],
                           "rules_version": "v1.0", "date_open": str(TODAY), "tier": c12["tier"]}])
fb12.submit_bracket(c12["symbol"], c12["shares"], "buy", c12["entry"], c12["target"], c12["stop"], client_order_id=tid)
n_before = len(fb12.orders)
sync.run()
t12 = [t for t in D.read_jsonl(D.TRADES) if t["trade_id"] == tid][0]
ok(t12["status"] == "pending" and t12["order_id"] and len(fb12.orders) == n_before,
   "sync adopts an order that was accepted before a crash (no duplicate order)")

print("sync: partial fills")
fb13 = fresh()
plan13 = score.run("decision")["plan"]
c13 = plan13[0]
place.place(c13["symbol"], *PLAN_TEXT, round(1 / (1 + c13["rr"]), 2), True)
t13 = D.read_jsonl(D.TRADES)[0]
o13 = fb13.orders[t13["order_id"]]
half = max(1, t13["shares"] // 2)
o13.update({"status": "partially_filled", "filled_qty": str(half), "filled_avg_price": str(t13["planned_entry"]),
            "filled_at": f"{TODAY}T15:00:00Z"})
fb13.pos[t13["symbol"]] = {"symbol": t13["symbol"], "current_price": str(t13["planned_entry"])}
sync.run()
t13 = D.read_jsonl(D.TRADES)[0]
ok(t13["status"] == "open" and t13["shares"] == half and t13.get("partial_fill"),
   "a partial fill is settled at the first sync (bracket legs don't protect a partial entry)")
ok(o13["status"] == "canceled", "the unfilled rest of the entry is cancelled")
ok(t13.get("exit_order_id") and fb13.orders[t13["exit_order_id"]]["qty"] == half,
   "the filled shares get a stop + target (OCO) for exactly the filled quantity")
ok(abs(t13["risk_dollars"] - half * abs(t13["entry_fill"] - t13["stop"])) < 0.02, "risk is recomputed for the filled size")
fb13.orders[t13["exit_order_id"]]["legs"][0].update({"status": "filled", "filled_avg_price": str(t13["stop"]),
                                                      "filled_at": f"{TODAY}T19:00:00Z"})
sync.run()
t13 = D.read_jsonl(D.TRADES)[0]
ok(t13["status"] == "closed" and t13["exit_reason"] == "stop" and t13["pnl"] == round(-half * abs(t13["entry_fill"] - t13["stop"]), 2),
   "an OCO stop fill is recorded with P/L on the filled shares")


def open_old_trade(b, sym="XLF", days=20, price=50.0, now=51.0):
    b.n += 1
    oid = f"ord{b.n}"
    b.orders[oid] = {"id": oid, "symbol": sym, "status": "filled", "filled_avg_price": str(price), "filled_at": "x",
                     "filled_qty": "10", "client_order_id": f"old-{sym}",
                     "legs": [{"id": oid + "tp", "type": "limit", "status": "new"}, {"id": oid + "sl", "type": "stop", "status": "new"}]}
    b.pos[sym] = {"symbol": sym, "current_price": str(now)}
    old = (TODAY - dt.timedelta(days=days)).isoformat()
    row = {"trade_id": f"old-{sym}", "order_id": oid, "rules_version": "v1.0", "date_open": old, "date_fill": old,
           "symbol": sym, "direction": "long", "tier": "B", "score": 5, "entry_fill": price, "stop": price - 1,
           "target": price + 3, "shares": 10, "risk_dollars": 10, "p_target": 0.3, "thesis": "t" * 12,
           "invalidation": "i" * 12, "dissent": "d" * 12, "status": "open"}
    D.append_jsonl(D.TRADES, [row])
    return oid


print("sync: time stop safety")
fb14 = fresh()
oid14 = open_old_trade(fb14)
fb14.close_fails = True
res = sync.run(close_mode=True)
t14 = D.read_jsonl(D.TRADES)[0]
ok(t14["status"] == "open" and t14.get("time_stop_due"), "a failed market close leaves the trade open and flagged")
ok(t14.get("exit_order_id") and fb14.orders[t14["exit_order_id"]]["legs"][0]["stop_price"] == t14["stop"],
   "the original stop and target are put back (the position is never left unprotected)")
ok(any("TIME STOP FAILED" in a for a in res["alerts"]), "the failure is an ALERT for Chris")
fb14.close_fails = False
sync.run(close_mode=True)
t14 = D.read_jsonl(D.TRADES)[0]
ok(t14["status"] == "closed" and t14["exit_reason"] == "time", "the next CLOSE run retries the time stop and closes it")
ok(not any("POSITION HAS NO STOP" in a for a in res["alerts"]), "and the alerts never contradict themselves")
ok(t14["exit_order_id"] in fb14.cancelled, "the replacement OCO is cancelled before the retry closes the position")

fb15 = fresh()
open_old_trade(fb15)
fb15.cancel_stuck = True
res = sync.run(close_mode=True)
t15 = D.read_jsonl(D.TRADES)[0]
ok(t15["status"] == "open" and "XLF" in fb15.pos, "if the stop/target won't cancel, the position is not closed out from under them")
ok(any("would not cancel" in a for a in res["alerts"]), "and Chris is alerted")

fb16 = fresh()
oid16 = open_old_trade(fb16)
real_cancel = fb16.cancel_order


def cancel_but_stop_fills(oid):
    if oid.endswith("sl"):
        leg = fb16.orders[oid16]["legs"][1]
        leg.update({"status": "filled", "filled_avg_price": "49.0", "filled_at": f"{TODAY}T19:50:00Z"})
        return
    real_cancel(oid)


fb16.cancel_order = cancel_but_stop_fills
sync.run(close_mode=True)
t16 = D.read_jsonl(D.TRADES)[0]
ok(t16["status"] == "closed" and t16["exit_reason"] == "stop" and t16["result_R"] == -1.0,
   "if the stop fills while the time stop is cancelling it, the stop is the recorded exit (no double close)")

fb17 = fresh()
open_old_trade(fb17)
fb17.is_open = False
res = sync.run(close_mode=True)
t17 = D.read_jsonl(D.TRADES)[0]
ok(t17["status"] == "open" and t17.get("time_stop_due") and not fb17.cancelled,
   "after the closing bell, a due time stop waits (stop/target untouched)")

fb18 = fresh()
open_old_trade(fb18)
fb18.calendar_fails = True
res = sync.run(close_mode=True)
ok(any("time stop was NOT checked" in a for a in res["alerts"]), "no calendar: days held is not silently 0, Chris is alerted")
fb18.calendar_fails = False

fb19 = fresh()
open_old_trade(fb19, "XLF")
oid_b = open_old_trade(fb19, "XLI", days=1)
fb19.order_fails = {fb19.orders[next(iter(fb19.orders))]["id"]}
fb19.orders[oid_b]["legs"][0].update({"status": "filled", "filled_avg_price": "53.0", "filled_at": f"{TODAY}T18:00:00Z"})
res = sync.run()
rows19 = {t["symbol"]: t for t in D.read_jsonl(D.TRADES)}
ok(rows19["XLI"]["status"] == "closed" and rows19["XLF"]["status"] == "open",
   "one trade's error doesn't stop (or un-save) the others")
ok(any("sync error" in a for a in res["alerts"]), "the failing trade is reported, not swallowed")

fb20 = fresh()
open_old_trade(fb20)
rows20 = D.read_jsonl(D.TRADES)
fb20.orders["closeX"] = {"id": "closeX", "symbol": "XLF", "status": "canceled", "legs": []}
rows20[0].update({"status": "closing", "close_order_id": "closeX"})
D.write_jsonl(D.TRADES, rows20)
res = sync.run()
t20 = D.read_jsonl(D.TRADES)[0]
ok(t20["status"] == "open" and t20.get("exit_order_id") and t20.get("time_stop_due"),
   "a time-stop close order that gets cancelled puts the stop back and retries (never stuck in 'closing')")

print("review details")
fresh()
wk = [{"trade_id": f"w{i}", "rules_version": "v1.0", "date_open": (TODAY - dt.timedelta(days=d)).isoformat(), "symbol": "XLE",
       "direction": "long", "tier": "A", "score": 7, "status": "open", "risk_dollars": 1, "thesis": "x" * 12,
       "invalidation": "y" * 12, "dissent": "z" * 12, "p_target": 0.3} for i, d in enumerate((0, 6, 7, 8))]
D.write_jsonl(D.TRADES, wk)
rep_w = review.run(offline=True)
ok("This week: 2 opened" in rep_w, "'this week' is exactly 7 days (a trade from 7 days ago is last week)")
cal_rows = [dict(wk[0], trade_id="cal1", status="closed", exit_reason="time", result_R=0.2, p_base=0.33, date_close=str(TODAY)),
            dict(wk[0], trade_id="cal2", status="closed", exit_reason="target", result_R=2.0, p_base=0.33, date_close=str(TODAY)),
            dict(wk[0], trade_id="cal3", status="closed", exit_reason="stop", result_R=-1.0, p_target=0.5, date_close=str(TODAY))]
D.write_jsonl(D.TRADES, cal_rows)
rep_c = review.run(offline=True)
ok("on 1 trades" in rep_c and "1 time-stop exits" in rep_c and "1 trades placed before the base-rate rule" in rep_c,
   "calibration leaves out time-stop exits and pre-rule trades, and says so")

print("broker plumbing")
os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"] = "PKTEST", "SECRET"
rb_ = BR.RestBroker()
retry = rb_.s.get_adapter("https://paper-api.alpaca.markets").max_retries
ok("GET" in retry.allowed_methods and "POST" not in retry.allowed_methods and "DELETE" not in retry.allowed_methods,
   "GETs are retried with backoff; order POSTs/DELETEs never are (no accidental double orders)")


def boom(*a, **k):
    import requests
    raise requests.ConnectionError("network down")


rb_.s.request = boom
try:
    rb_.account()
    net_ok = False
except BR.BrokerError as e:
    net_ok = "network error" in str(e)
ok(net_ok, "a network failure surfaces as a BrokerError the scripts handle, not a raw crash")
del os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"]



print("second-review regressions")
fb40 = fresh()
open_old_trade(fb40, days=2)
oid40 = next(iter(fb40.orders))
for l in fb40.orders[oid40]["legs"]:
    l["status"] = "canceled"                 # someone cancelled the stop/target by hand
res = sync.run()
t40 = D.read_jsonl(D.TRADES)[0]
ok(t40.get("exit_order_id") and any("had NO live stop" in a for a in res["alerts"]),
   "a held position whose stop/target died gets them put back on the next sync, with an alert")
fb40.orders[t40["exit_order_id"]]["status"] = "rejected"
fb40.orders[t40["exit_order_id"]]["legs"][0]["status"] = "rejected"
res = sync.run()
t40b = D.read_jsonl(D.TRADES)[0]
ok(t40b["exit_order_id"] != t40["exit_order_id"], "a protective OCO that is later rejected is noticed and replaced")

fb41 = fresh()
oid41 = open_old_trade(fb41)
real_order = fb41.order
calls41 = {"n": 0}
def flaky_order(oid):
    if oid == oid41 and any(x.endswith("sl") for x in fb41.cancelled):
        calls41["n"] += 1
        if calls41["n"] >= 1:
            raise BR.BrokerError("GET order -> network error: ReadTimeout")
    return real_order(oid)
fb41.order = flaky_order
res = sync.run(close_mode=True)
t41 = D.read_jsonl(D.TRADES)[0]
ok("XLF" in fb41.pos and t41.get("exit_order_id") and any("TIME STOP FAILED" in a for a in res["alerts"]),
   "a time stop that crashes after cancelling the stop puts the stop back and says so")

fb42 = fresh()
open_old_trade(fb42)
fb42.close_lost_reply = True
res = sync.run(close_mode=True)
t42 = D.read_jsonl(D.TRADES)[0]
ok(t42["status"] == "closed" and t42["exit_reason"] == "time" and not fb42.ocos,
   "a close that went through but lost its reply is confirmed from order history (no stray OCO, no stuck trade)")

fb43 = fresh()
score.run("decision")
(TMP / "reports" / f"plan_{TODAY}.json").unlink()
expect_exit(lambda: score.run("decision"), "DECISION ALREADY RAN", "deleting the plan file can't hide that DECISION ran")
r43 = score.run("decision", force=True)
r44 = score.run("decision", force=True)
runs = {c["run"] for c in D.read_jsonl(D.CANDIDATES)}
ok(runs == {"decision", "decision-rerun1", "decision-rerun2"}, f"forced reruns are numbered from the journal ({sorted(runs)})")

fb45 = fresh()
os.environ["FINNHUB_API_KEY"] = "tok"
score._finnhub_get = lambda session, sym, start, end, key: [] if sym == "AAPL" else [{"symbol": sym, "date": (TODAY + dt.timedelta(days=70)).isoformat()}]
bl45, ver45, note45 = score.earnings_blackout(fb45, TODAY.isoformat())
ok("AAPL" not in ver45 and "MSFT" in ver45 and "no upcoming date" in note45,
   "an empty earnings answer is 'unknown', not 'no earnings' (verified only when a next date is found)")
write_regime({"exclude": {"MSFT": f"earnings {D.trading_days_ahead(fb45, str(TODAY), 1)} (made up)"}})
r45 = score.run("decision")
score._finnhub_get = real_fh
del os.environ["FINNHUB_API_KEY"]
ok(any("EXCLUSION CONFLICT: MSFT" in w for w in r45["warnings"]), "excluding a stock the calendar shows clean is flagged loudly")
ok([c for c in D.read_jsonl(D.CANDIDATES) if c["symbol"] == "MSFT"][0]["exclusion_conflict"], "and recorded for the weekly review")


print("final-review regressions")
ok(place.rr_matches(29.09, 28.86, 29.56, 2.0), "the real UUP plan (29.09 / 28.86 / 29.56, rr 2.0) is no longer refused by cent rounding")
ok(not place.rr_matches(29.09, 28.86, 29.75, 2.0), "but a target moved by hand still fails the R:R check")

fb50 = fresh()
r50 = score.run("decision")
sup = TMP / "reports" / f"plan_{TODAY}.json"
first_plan = sup.read_text()
score.run("decision", force=True)
sup.write_text(first_plan)              # restore the superseded plan by hand
c50 = r50["plan"][0]
expect_exit(lambda: place.place(c50["symbol"], *PLAN_TEXT, round(1 / (1 + c50["rr"]), 2), True), "not the plan from today",
            "restoring a superseded plan after a rerun is refused (only the latest run counts)")

fb51 = fresh()
r51 = score.run("decision")
pf51 = TMP / "reports" / f"plan_{TODAY}.json"
pj51 = json.loads(pf51.read_text())
victim = pj51["plan"][0]
victim["shares"] = victim["shares"] * 40
text51 = json.dumps(pj51, indent=2)
pf51.write_text(text51)
rows51 = D.read_jsonl(D.CANDIDATES)
for r_ in rows51:                       # forge the journal to match (an agent that can write journal/)
    r_["plan_sha"] = __import__("hashlib").sha256(text51.encode()).hexdigest()
    if r_["symbol"] == victim["symbol"]:
        r_["shares"] = victim["shares"]
D.write_jsonl(D.CANDIDATES, rows51)
expect_exit(lambda: place.place(victim["symbol"], *PLAN_TEXT, round(1 / (1 + victim["rr"]), 2), True), "larger than the risk rules allow",
            "even a plan forged together with the journal can't oversize an order")
ok(not fb51.orders, "nothing was sent")

fb52 = fresh()
r52 = score.run("decision")
for i, sym in enumerate(("AAA", "BBB", "CCC", "DDD", "EEE")):
    fb52.pos[sym] = {"symbol": sym, "current_price": "10"}
c52 = r52["plan"][0]
expect_exit(lambda: place.place(c52["symbol"], *PLAN_TEXT, round(1 / (1 + c52["rr"]), 2), True), "risk rule",
            "placement re-checks the live risk limits (5 positions already open)")

fb53 = fresh()
oid53 = open_old_trade(fb53, days=2)
for l in fb53.orders[oid53]["legs"]:
    l["status"] = "canceled"
D.write_jsonl(D.TRADES, [dict(D.read_jsonl(D.TRADES)[0], exit_order_ids=["oco_x"], exit_order_id="oco_x")])
fb53.orders["oco_x"] = {"id": "oco_x", "symbol": "XLF", "type": "limit", "status": "new",
                        "legs": [{"id": "oco_xsl", "type": "stop", "status": "new"}]}
real53 = fb53.order
seen53 = {"n": 0}
def racy(oid):
    o = real53(oid)
    if oid == "oco_x":
        seen53["n"] += 1
        if seen53["n"] == 1:           # first read: still live... then the target fills before the next read
            snap = json.loads(json.dumps(o))
            fb53.orders["oco_x"].update({"status": "filled", "filled_avg_price": "53.0", "filled_at": f"{TODAY}T18:30:00Z"})
            fb53.orders["oco_x"]["legs"][0]["status"] = "canceled"
            fb53.pos.pop("XLF", None)
            return snap
    return o
fb53.order = racy
sync.run()
fb53.order = real53
sync.run()
t53 = D.read_jsonl(D.TRADES)[0]
ok(t53["status"] == "closed" and t53["exit_reason"] == "target" and not fb53.ocos,
   "an exit that fills mid-check is recorded, and no new OCO is placed on a flat position")

fb54 = fresh()
open_old_trade(fb54, days=2)
oid54 = next(iter(fb54.orders))
for l in fb54.orders[oid54]["legs"]:
    l["status"] = "canceled"
real_oco = fb54.submit_oco
def lost_oco(*a, **k):
    real_oco(*a, **k)
    raise BR.BrokerError("POST /v2/orders -> network error: ReadTimeout")
fb54.submit_oco = lost_oco
res54 = sync.run()
sync.run()
live_ocos = [o for o in fb54.ocos if o["status"] == "new"]
ok(len(live_ocos) == 1, f"an OCO whose reply was lost is found by its client id, never doubled ({len(live_ocos)} live)")

fb55 = fresh()
p55 = score.run("decision")["plan"][0]
place.place(p55["symbol"], *PLAN_TEXT, round(1 / (1 + p55["rr"]), 2), True)
t55 = D.read_jsonl(D.TRADES)[0]
o55 = fb55.orders[t55["order_id"]]
o55.update({"status": "partially_filled", "filled_qty": "1", "filled_avg_price": str(t55["planned_entry"]),
            "filled_at": f"{TODAY}T15:00:00Z"})
fb55.pos[t55["symbol"]] = {"symbol": t55["symbol"], "current_price": str(t55["planned_entry"])}
real_cancel55 = fb55.cancel_order
def cancel55(oid):
    if oid.endswith("tp") or oid.endswith("sl"):
        return                          # the old legs refuse to die
    if oid == o55["id"]:
        o55["status"] = "canceled"      # parent cancels, legs stay live
        return
    real_cancel55(oid)
fb55.cancel_order = cancel55
for l in o55["legs"]:
    l["status"] = "new"
res55 = sync.run()
ok(not fb55.ocos and any("would not cancel" in a for a in res55["alerts"]),
   "after a partial fill, stuck old legs mean no second exit set is added (and Chris is told)")

fb56 = fresh()
p56 = score.run("decision")["plan"][0]
place.place(p56["symbol"], *PLAN_TEXT, round(1 / (1 + p56["rr"]), 2), True)
t56 = D.read_jsonl(D.TRADES)[0]
fb56.orders[t56["order_id"]]["status"] = "rejected"
res56 = sync.run()
ok(D.read_jsonl(D.TRADES)[0]["status"] == "rejected" and any("rejected" in a for a in res56["alerts"]),
   "an order Alpaca rejects after accepting it is journaled as rejected (not 'unfilled')")

fb57 = fresh()
both57 = {s: {d: {"rr": 2.0, "entry": 10, "stop": 9 if d == "long" else 11, "target": 12 if d == "long" else 8}
              for d in ("long", "short")} for s in ("AAPL", "MSFT", "XLE")}
rows57, _ = control.draw(both57, {"AAPL", "MSFT"}, fb57, "2099-03-01", verified=set(D.STOCKS))
ok(all(r["symbol"] == "XLE" for r in rows57), "excluded or blacked-out stocks are never drawn as controls")

fb58 = fresh()
p58 = score.run("decision")["plan"][0]
place.place(p58["symbol"], *PLAN_TEXT, round(1 / (1 + p58["rr"]), 2), True)
t58 = D.read_jsonl(D.TRADES)[0]
fb58.orders[t58["order_id"]]["status"] = "replaced"
res58 = sync.run(close_mode=True)
ok(D.read_jsonl(D.TRADES)[0]["status"] == "pending" and any("edited by hand" in a for a in res58["alerts"]),
   "an entry edited by hand in the dashboard is flagged, not wrongly marked unfilled")

print("pentest regressions")
fb30 = fresh()
plan30 = score.run("decision")["plan"]
c30 = plan30[0]
pf = TMP / "reports" / f"plan_{TODAY}.json"
orig_plan = pf.read_text()
forged = json.loads(orig_plan)
forged["plan"][0]["shares"] = forged["plan"][0]["shares"] * 50
pf.write_text(json.dumps(forged, indent=2))
expect_exit(lambda: place.place(c30["symbol"], *PLAN_TEXT, round(1 / (1 + c30["rr"]), 2), True), "not the plan from today",
            "a hand-edited plan file (50x the shares) is refused")
ok(not fb30.orders, "and nothing was sent to the broker")
pf.write_text(orig_plan)
fb30.bars[c30["symbol"]][-1]["c"] *= 1.10
expect_exit(lambda: place.place(c30["symbol"], *PLAN_TEXT, round(1 / (1 + c30["rr"]), 2), True), "more than 3% from the planned entry",
            "an entry far from the live price is refused")
fb30.bars[c30["symbol"]][-1]["c"] /= 1.10
expect_exit(lambda: place.check_next_earnings(fb30, str(TODAY), None), "next_earnings", "earnings_checked without a next_earnings date is refused")
soon = D.trading_days_ahead(fb30, str(TODAY), 3)
expect_exit(lambda: place.check_next_earnings(fb30, str(TODAY), soon), "inside the 10-trading-day blackout",
            "a hand-checked earnings date inside the window is refused")
expect_exit(lambda: place.check_next_earnings(fb30, str(TODAY), "2020-01-01"), "in the past", "a past earnings date is refused")
later = (TODAY + dt.timedelta(days=60)).isoformat()
place.check_next_earnings(fb30, str(TODAY), later)
ok(True, "a hand-checked next earnings date beyond the window is accepted (a 'checked on' date in the note no longer confuses it)")

(TMP / ".env").write_text("HTTPS_PROXY=http://evil:8080\nREQUESTS_CA_BUNDLE=/tmp/evil.pem\nALPACA_API_KEY=PKFROMENV\n")
for k in ("HTTPS_PROXY", "REQUESTS_CA_BUNDLE", "ALPACA_API_KEY"):
    os.environ.pop(k, None)
real_kc = D.keychain_get
D.keychain_get = lambda name: None
D.load_env()
D.keychain_get = real_kc
ok("HTTPS_PROXY" not in os.environ and "REQUESTS_CA_BUNDLE" not in os.environ and os.environ.get("ALPACA_API_KEY") == "PKFROMENV",
   "a planted .env can only supply the three key names (no proxy / CA bundle to siphon the keys)")
os.environ.pop("ALPACA_API_KEY", None)
(TMP / ".env").unlink()
ok(BR.retrying_session().trust_env is False, "HTTP sessions ignore proxy and CA settings from the environment")


class Redirect:
    status_code, text = 302, ""


os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"] = "PKTEST", "SECRET"
rb2 = BR.RestBroker()
rb2.s.request = lambda *a, **k: Redirect()
try:
    rb2.account()
    redir = False
except BR.BrokerError as e:
    redir = "redirect" in str(e)
ok(redir, "a redirect is refused, so the key headers are never sent to another host")
del os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"]

outside_reg = TMP / "regime_evil.json"
outside_reg.write_text("{}")
expect_exit(lambda: score.load_regime(str(outside_reg)), "must be reports/regime_", "a regime file outside reports/ is refused")
deep = TMP / "reports" / "regime_deep.json"
deep.write_text("[" * 100000 + "]" * 100000)
expect_exit(lambda: score.load_regime(str(deep)), "REGIME FILE INVALID", "a pathological regime file is a clean refusal, not a crash")
deep.unlink()

fb31 = fresh()
write_regime({"exclude": {"AAPL": f"earnings {D.trading_days_ahead(fb31, str(TODAY), 1)} [click](http://evil.example) <img src=x onerror=alert(1)>"}})
score.run("decision")
D.write_jsonl(D.TRADES, [])
review.run(offline=True)
wk_txt = D.WEEKLY.read_text()
ok("](http" not in wk_txt and "<img" not in wk_txt, "agent or web text can't inject links or HTML into weekly.md")

fb32 = fresh()
plan32 = score.run("decision")["plan"]
c32 = plan32[0]
place.place(c32["symbol"], *PLAN_TEXT, round(1 / (1 + c32["rr"]), 2), True)
rows32 = D.read_jsonl(D.TRADES)
rows32[0]["date_open"] = (TODAY - dt.timedelta(days=3)).isoformat()
D.write_jsonl(D.TRADES, rows32)
sync.run()
ok(D.read_jsonl(D.TRADES)[0]["status"] == "unfilled" and rows32[0]["order_id"] in fb32.cancelled,
   "an entry left over from an earlier day is cancelled by any sync (never fills days later at a stale price)")

fb33 = fresh()
oid33 = open_old_trade(fb33, days=2)
fb33.orders[oid33]["legs"][0].update({"status": "filled", "filled_avg_price": "53.0", "filled_at": f"{TODAY}T18:00:00Z"})
sync.run()
rows33 = D.read_jsonl(D.TRADES)
rows33[0]["exit_price"], rows33[0]["result_R"] = 60.0, 10.0       # someone "improves" the result by hand
D.write_jsonl(D.TRADES, rows33)
issues33 = review.verify_against_broker(rows33)
ok(any("matches no Alpaca exit fill" in i for i in issues33), "an edited result in the journal is caught against Alpaca's records")

lock_target = TMP / "victim.txt"
lock_target.write_text("keep me")
D.LOCK_FILE.unlink(missing_ok=True)
os.symlink(lock_target, D.LOCK_FILE)
try:
    with D.journal_lock():
        pass
    sym_ok = False
except OSError:
    sym_ok = True
ok(sym_ok and lock_target.read_text() == "keep me", "the lock file refuses to follow a planted symlink")
D.LOCK_FILE.unlink()
D.write_jsonl(D.TRADES, [{"x": 1}])
ok(not list((TMP / "journal").glob("*.tmp")), "atomic writes leave no temp files behind")

shutil.rmtree(TMP)
print(f"\nALL {passed} CHECKS PASSED")
