"""Place one planned bracket order from today's plan and log it to journal/trades.jsonl.

Usage:
  python scripts/place.py --symbol XLE --p-target 0.42 \
      --thesis "Energy leadership with oil > $100 and rates high" \
      --invalidation "Daily close below 92.10" \
      --dissent "Fails if Brent drops under $98 on Russia supply news"
  python scripts/place.py --smoke-test        # 1-share SPY bracket, confirm, then cancel
Options:
  --earnings-checked   required for a stock whose earnings date could not be verified automatically
  --dry-run            show what would be sent without sending it
"""
import argparse
import json
import sys
import time

import desk as D


def confirm(b, order_id, tries=5):
    for _ in range(tries):
        try:
            o = b.order(order_id)
            if o.get("status"):
                return o
        except Exception:
            pass
        time.sleep(1)
    return None


def smoke_test():
    b = D.broker()
    px = b.latest_prices(["SPY"])["SPY"]
    last = px["last"] or px["ask"]
    if not last:
        sys.exit("SMOKE TEST FAILED: no SPY price from Alpaca data API")
    limit = round(last * 0.95, 2)   # deliberately below market so it won't fill
    o = b.submit_bracket("SPY", 1, "buy", limit, round(limit * 1.05, 2), round(limit * 0.97, 2),
                         client_order_id=f"smoke-{int(time.time())}")
    got = confirm(b, o["id"])
    if not got:
        sys.exit(f"SMOKE TEST FAILED: order {o['id']} submitted but could not be confirmed")
    b.cancel_order(o["id"])
    print(f"SMOKE TEST PASSED: paper bracket order {o['id']} reached status '{got['status']}' and was cancelled.")


def place(symbol, thesis, invalidation, dissent, p_target, earnings_checked=False, dry_run=False):
    today = D.today_str()
    plan_file = D.REPORTS / f"plan_{today}.json"
    if not plan_file.exists():
        sys.exit(f"REFUSED: no plan for today ({plan_file.name}). Run score.py --mode decision first.")
    plan = json.loads(plan_file.read_text()).get("plan", [])
    c = next((x for x in plan if x["symbol"] == symbol), None)
    if not c:
        sys.exit(f"REFUSED: {symbol} is not in today's plan. Only planned orders can be placed.")
    if not (0.0 < p_target < 1.0):
        sys.exit("REFUSED: --p-target must be between 0 and 1 (exclusive).")
    for name, val in (("thesis", thesis), ("invalidation", invalidation), ("dissent", dissent)):
        if not val or len(val.strip()) < 10:
            sys.exit(f"REFUSED: --{name} must be a real sentence.")
    if c.get("earnings_unverified") and not earnings_checked:
        sys.exit(f"REFUSED: {symbol} earnings date unverified. Check it; if no earnings in 10 trading days, rerun with --earnings-checked.")
    if any(t["symbol"] == symbol for t in D.open_trades()):
        sys.exit(f"REFUSED: {symbol} already has an open or pending trade in the journal.")

    side = "buy" if c["direction"] == "long" else "sell"
    trade_id = f"{today}-{symbol}-{c['direction']}"
    order_args = dict(symbol=symbol, qty=c["shares"], side=side, limit_price=c["entry"],
                      take_profit=c["target"], stop_price=c["stop"], client_order_id=trade_id)
    if dry_run:
        print("DRY RUN, nothing sent:", json.dumps(order_args))
        return

    b = D.broker()
    o = b.submit_bracket(**order_args)
    got = confirm(b, o["id"])
    if not got:
        sys.exit(f"ORDER NOT CONFIRMED: submitted {o.get('id')} but status could not be read. Do NOT report it as placed.")

    filled = got["status"] == "filled"
    D.append_jsonl(D.TRADES, [{
        "trade_id": trade_id, "order_id": got["id"], "rules_version": D.rules_version(), "date_open": today,
        "symbol": symbol, "direction": c["direction"], "tier": c["tier"], "quota": c.get("quota", False),
        "score": c["score"], "breakdown": c["breakdown"], "planned_entry": c["entry"],
        "entry_fill": float(got["filled_avg_price"]) if filled and got.get("filled_avg_price") else None,
        "date_fill": today if filled else None, "stop": c["stop"], "target": c["target"], "shares": c["shares"],
        "risk_dollars": c["risk_dollars"], "p_target": p_target, "thesis": thesis, "invalidation": invalidation,
        "dissent": dissent, "status": "open" if filled else "pending", "date_close": None, "exit_price": None,
        "exit_reason": None, "result_R": None, "pnl": None}])
    print(f"PLACED {symbol} {c['direction'].upper()} tier {c['tier']} | {c['shares']} sh | entry {c['entry']} "
          f"stop {c['stop']} target {c['target']} | ${c['risk_dollars']} at risk | order {got['id']} status {got['status']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol")
    ap.add_argument("--thesis")
    ap.add_argument("--invalidation")
    ap.add_argument("--dissent")
    ap.add_argument("--p-target", type=float)
    ap.add_argument("--earnings-checked", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--smoke-test", action="store_true")
    a = ap.parse_args()
    if a.smoke_test:
        smoke_test()
    else:
        if not a.symbol or a.p_target is None:
            ap.error("--symbol and --p-target are required")
        place(a.symbol.upper(), a.thesis, a.invalidation, a.dissent, a.p_target, a.earnings_checked, a.dry_run)
