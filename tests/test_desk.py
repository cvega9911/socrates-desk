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
import score, place, sync, review, control   # noqa: E402

TODAY = dt.date.fromisoformat(D.today_str())


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

    def account(self):
        return {"equity": str(self.equity), "last_equity": str(self.last_equity)}

    def positions(self):
        return list(self.pos.values())

    def open_orders(self):
        return [o for o in self.orders.values() if o["status"] in ("new", "accepted")]

    def asset(self, s):
        return {"shortable": True, "easy_to_borrow": s != "GDX"}

    def calendar(self, a, b):
        a, b = dt.date.fromisoformat(a), dt.date.fromisoformat(b)
        return [str(a + dt.timedelta(days=i)) for i in range((b - a).days + 1) if (a + dt.timedelta(days=i)).weekday() < 5]

    def portfolio_history(self, period="1A"):
        return {"equity": [], "timestamp": []}

    def submit_bracket(self, symbol, qty, side, limit_price, take_profit, stop_price, client_order_id=None):
        self.n += 1
        oid = f"ord{self.n}"
        self.orders[oid] = {"id": oid, "symbol": symbol, "qty": qty, "side": side, "status": "new", "limit": limit_price,
                            "legs": [{"id": oid + "tp", "type": "limit", "status": "held", "limit_price": take_profit},
                                     {"id": oid + "sl", "type": "stop", "status": "held", "stop_price": stop_price}]}
        return {"id": oid}

    def order(self, oid):
        return self.orders[oid]

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        for o in self.orders.values():
            if o["id"] == oid:
                o["status"] = "canceled"
            for l in o["legs"]:
                if l["id"] == oid:
                    l["status"] = "canceled"

    def close_position(self, s):
        self.n += 1
        oid = f"close{self.n}"
        px = float(self.pos[s]["current_price"])
        self.orders[oid] = {"id": oid, "symbol": s, "status": "filled", "filled_avg_price": str(px),
                            "filled_at": f"{TODAY}T19:55:00Z", "legs": []}
        del self.pos[s]
        return {"id": oid}

    def daily_bars(self, symbols, start, end):
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
        o.update({"status": "filled", "filled_avg_price": str(price), "filled_at": f"{TODAY}T14:20:00Z"})
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
rr_again = score.run("decision")
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
    ok("already has an open" in str(e), "refuses duplicate placement")
ok(all("p_base" in t and abs(t["p_base"] - 1 / (1 + c["rr"])) < 0.001 for t, c in zip(D.read_jsonl(D.TRADES), plan)), "p_base logged on every trade")

# rerun decision: symbols now busy, should not re-plan them
r2 = score.run("decision")
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
                       "p_target": 0.6 if win else 0.3, "thesis": "x" * 20, "invalidation": "y" * 20, "dissent": "z" * 20,
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
ok("beats" in rep and "Verdict:" in rep, "baseline verdict shown once 20 real + 50 controls exist")
ok("Brier score" in rep and "Brier skill" in rep and "neutral base rate" in rep, "calibration reported with the base-rate anchor check")
ok("5c. Does the score predict forward R" in rep and "matured candidate-trades" in rep, "score study section reports matured candidates")
ok("Factor regime:" in rep and "Score 7:" in rep and "Significance test withheld" in rep, "score buckets and factor lines shown; significance withheld on small samples")
ok("Matured cohorts (25 real vs 80 controls)" in rep, "baseline verdict uses matured cohorts only")
ok("planned: " in rep and "resolved" in rep, "shadow book simulated")
ok("Sample too small" not in rep, "no small-sample warning at 25 trades")
ok("planned but never placed" in rep, "flags planned-but-not-placed candidates")
ok(D.WEEKLY.read_text().count("## Weekly review") == 1, "appended to weekly.md")
D.write_jsonl(D.CONTROL, D.read_jsonl(D.CONTROL)[:10])
rep_off = review.run(offline=True)
ok("Skipped" in rep_off, "offline mode works without Alpaca")
ok("Verdict withheld" in rep_off, "verdict withheld when there are too few controls")
rr_ = __import__("random").Random(11)
pos = [{"date": f"d{d}", "score": sc, "R": (sc - 5) * 0.3 + rr_.gauss(0, 0.5)} for d in range(30) for sc in range(3, 9)]
neg = [{"date": f"d{d}", "score": sc, "R": rr_.gauss(0, 1.0)} for d in range(30) for sc in range(3, 9)]
r1, p1 = review.within_day_perm_p(pos, 500)
r2, p2 = review.within_day_perm_p(neg, 500)
ok(r1 > 0.5 and p1 < 0.01, f"permutation test detects a real score->R relationship (r={r1:.2f}, p={p1:.3f})")
ok(abs(r2) < 0.3 and p2 > 0.01, f"permutation test stays quiet on noise (r={r2:.2f}, p={p2:.3f})")
young = [{"date": str(TODAY - dt.timedelta(days=3)), "symbol": "SPY", "direction": "long", "score": 8, "entry": 100.0,
          "stop": 1.0, "target": 100.01, "breakdown": {}}]
ok(review.score_study(young, {"SPY": D.bars_to_df(fr.bars["SPY"])}) == [], "candidates younger than 10 trading days are excluded (no early-exit bias)")
pc_hi = control.percentile_vs_random(5.0, [-1.0, 2.0, -1.0], 25)
pc_lo = control.percentile_vs_random(-5.0, [-1.0, 2.0, -1.0], 25)
ok(pc_hi > 0.99 and pc_lo < 0.01, "percentile function separates clearly better / worse than random")
ok("no evidence" in control.verdict(0.5) and "evidence of skill" in control.verdict(0.97) and "hurting" in control.verdict(0.02), "verdict wording")

shutil.rmtree(TMP)
print(f"\nALL {passed} CHECKS PASSED")
