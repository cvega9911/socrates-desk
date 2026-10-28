"""Place one planned bracket order from today's plan and log it to journal/trades.jsonl.

Usage (preferred): write the order file with your file tool, then pass only its path on the command line.
  reports/order_XLE_2026-10-28.json:
    {"symbol": "XLE", "p_target": 0.32,
     "thesis": "Energy leadership with Brent above $100 and the regime file at 2 for energy",
     "invalidation": "Daily close below 92.10",
     "dissent": "Fails if Brent drops under $98 on Russia supply news",
     "earnings_checked": false}
  python scripts/place.py --input reports/order_XLE_2026-10-28.json

Free text never goes through the shell this way, so "$100" stays "$100" and nothing in a web quote can run.

Legacy (still accepted, flagged as text_source "cli" in the journal): --symbol/--p-target/--thesis/--invalidation/
--dissent on the command line. Shell expansion can silently change that text, so don't use it.

  python scripts/place.py --smoke-test        # 1-share SPY bracket far below market, confirm, then cancel
Options:
  --earnings-checked   (legacy) same as "earnings_checked": true in the order file
  --dry-run            show what would be sent without sending it
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

import broker as BR
import desk as D

LIVE = {"new", "accepted", "pending_new", "accepted_for_bidding", "held", "partially_filled", "filled",
        "pending_replace", "replaced", "calculated", "done_for_day"}
DEAD = {"rejected", "canceled", "expired", "suspended", "stopped", "pending_cancel"}
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
sleep = time.sleep      # tests replace this


def confirm(b, order_id, tries=5):
    """The order's status once Alpaca reports one, or None."""
    for _ in range(tries):
        try:
            o = b.order(order_id)
            if o.get("status"):
                return o
        except Exception:
            pass
        sleep(1)
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
    try:
        b.cancel_order(o["id"])
    except Exception as e:
        sys.exit(f"SMOKE TEST: order {o['id']} was placed but the cancel FAILED ({e}). Cancel it in the Alpaca "
                 "dashboard now, or it will block SPY.")
    if not got:
        sys.exit(f"SMOKE TEST FAILED: order {o['id']} submitted but could not be confirmed (cancel was sent)")
    print(f"SMOKE TEST PASSED: paper bracket order {o['id']} reached status '{got['status']}' and was cancelled.")


def clean_text(name, val):
    if not isinstance(val, str):
        raise D.DeskError(f"REFUSED: {name} must be text.")
    val = " ".join(_CTRL.sub(" ", val).split())
    if len(val) < 10:
        raise D.DeskError(f"REFUSED: {name} must be a real sentence.")
    if len(val) > D.TEXT_MAX:
        raise D.DeskError(f"REFUSED: {name} is {len(val)} characters; keep it under {D.TEXT_MAX}.")
    return val


def load_order_file(path):
    p = Path(path)
    try:
        rp = p.resolve(strict=True)
    except (OSError, RuntimeError):
        raise D.DeskError(f"REFUSED: order file {path} not found.")
    if rp.parent != D.REPORTS.resolve() or rp.suffix != ".json" or not rp.name.startswith("order_"):
        raise D.DeskError("REFUSED: the order file must be reports/order_<SYMBOL>_<date>.json inside the desk folder.")
    if rp.stat().st_size > 20_000:
        raise D.DeskError("REFUSED: order file is too large.")
    try:
        data = json.loads(rp.read_text())
    except json.JSONDecodeError as e:
        raise D.DeskError(f"REFUSED: order file is not valid JSON ({e}).")
    if not isinstance(data, dict):
        raise D.DeskError("REFUSED: order file must be a JSON object.")
    extra = set(data) - {"symbol", "p_target", "thesis", "invalidation", "dissent", "earnings_checked", "earnings_note"}
    if extra:
        raise D.DeskError(f"REFUSED: unknown keys in order file: {', '.join(sorted(extra))}.")
    return data


def _update(trade_id, **fields):
    rows = D.read_jsonl(D.TRADES)
    for r in rows:
        if r.get("trade_id") == trade_id:
            r.update(fields)
    D.write_jsonl(D.TRADES, rows)


def place(symbol, thesis, invalidation, dissent, p_target, earnings_checked=False, dry_run=False, text_source="file",
          earnings_note=None):
    with D.journal_lock():
        return _place(symbol, thesis, invalidation, dissent, p_target, earnings_checked, dry_run, text_source,
                      earnings_note)


def _place(symbol, thesis, invalidation, dissent, p_target, earnings_checked, dry_run, text_source, earnings_note):
    today = D.today_str()
    rv = D.rules_version()
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z]{1,5}", symbol or ""):
        raise D.DeskError("REFUSED: symbol must be 1-5 capital letters.")
    plan_file = D.REPORTS / f"plan_{today}.json"
    if not plan_file.exists():
        raise D.DeskError(f"REFUSED: no plan for today ({plan_file.name}). Run score.py --mode decision first.")
    pj = json.loads(plan_file.read_text())
    if pj.get("date") != today or pj.get("rules_version") != rv:
        raise D.DeskError("REFUSED: the plan file is not today's plan for this rules version.")
    c = next((x for x in pj.get("plan", []) if x["symbol"] == symbol), None)
    if not c:
        raise D.DeskError(f"REFUSED: {symbol} is not in today's plan. Only planned orders can be placed.")
    if isinstance(p_target, bool) or not isinstance(p_target, (int, float)) or not (0.0 < p_target < 1.0):
        raise D.DeskError("REFUSED: p_target must be a number between 0 and 1 (exclusive).")
    base = 1.0 / (1.0 + c["rr"])              # hit-the-target probability in a zero-edge market
    lo, hi = max(0.05, base - 0.15), min(0.95, base + 0.15)
    if not (lo - 1e-9 <= p_target <= hi + 1e-9):
        raise D.DeskError(f"REFUSED: p_target {p_target} is outside the allowed range {lo:.2f}-{hi:.2f}. The neutral-market "
                          f"base rate for R:R {c['rr']} is {base:.2f}. Start there and adjust by at most 15 points for reasons you can name.")
    thesis, invalidation, dissent = (clean_text(n, v) for n, v in
                                     (("thesis", thesis), ("invalidation", invalidation), ("dissent", dissent)))
    if c.get("earnings_unverified") and not earnings_checked:
        raise D.DeskError(f"REFUSED: {symbol} earnings date unverified. Check it; if no earnings in 10 trading days, set "
                          '"earnings_checked": true (and "earnings_note": "<date and source>") in the order file.')

    trade_id = f"{today}-{symbol}-{c['direction']}"
    rows = D.read_jsonl(D.TRADES)
    mine = [t for t in rows if t.get("trade_id") == trade_id]
    retry_submitting = bool(mine) and mine[-1].get("status") == "submitting"
    if mine and not retry_submitting:
        raise D.DeskError(f"REFUSED: {trade_id} is already in the journal (status {mine[-1].get('status')}). "
                          "Do not place it twice.")
    if not retry_submitting and any(t["symbol"] == symbol for t in D.open_trades()):
        raise D.DeskError(f"REFUSED: {symbol} already has an open or pending trade in the journal.")

    side = "buy" if c["direction"] == "long" else "sell"
    order_args = dict(symbol=symbol, qty=c["shares"], side=side, limit_price=c["entry"],
                      take_profit=c["target"], stop_price=c["stop"], client_order_id=trade_id)
    if dry_run:
        print("DRY RUN, nothing sent:", json.dumps(order_args))
        return

    b = D.broker()
    try:
        trading_day, is_open, close = D.market_status(b)
    except Exception as e:
        raise D.DeskError(f"MARKET CLOCK UNAVAILABLE ({type(e).__name__}). Nothing sent. Tell Chris.")
    if not (trading_day and is_open):
        raise D.DeskError("REFUSED: the market is not open right now. Nothing sent.")

    if not retry_submitting:
        D.append_jsonl(D.TRADES, [{
            "trade_id": trade_id, "order_id": None, "rules_version": rv, "date_open": today,
            "symbol": symbol, "direction": c["direction"], "tier": c["tier"], "quota": c.get("quota", False),
            "score": c["score"], "breakdown": c["breakdown"], "planned_entry": c["entry"], "entry_fill": None,
            "date_fill": None, "stop": c["stop"], "target": c["target"], "shares": c["shares"],
            "planned_shares": c["shares"], "risk_dollars": c["risk_dollars"],
            "equity_at_open": c.get("equity_at_plan"), "p_target": p_target, "p_base": round(base, 3),
            "thesis": thesis, "invalidation": invalidation, "dissent": dissent, "text_source": text_source,
            "earnings_checked": bool(earnings_checked), "earnings_note": earnings_note,
            "status": "submitting", "date_close": None, "exit_price": None, "exit_reason": None,
            "result_R": None, "pnl": None}])

    # Submit. If the request fails or times out, Alpaca may still have accepted it: look it up by our id.
    oid, err = None, None
    if retry_submitting:
        try:
            found = b.order_by_client_id(trade_id)
        except Exception as e:
            raise D.DeskError(f"ORDER STATUS UNKNOWN for {trade_id} ({type(e).__name__}). Do NOT report it as placed and "
                              "do not rerun place.py; sync.py will reconcile it. Tell Chris.")
        oid = found["id"] if found else None
    if oid is None:
        try:
            oid = b.submit_bracket(**order_args)["id"]
        except BR.BrokerError as e:
            err = str(e)
            try:
                found = b.order_by_client_id(trade_id)
            except Exception:
                raise D.DeskError(f"ORDER STATUS UNKNOWN for {trade_id}: submit failed ({err[:200]}) and the lookup "
                                  "failed too. Do NOT report it as placed and do not rerun place.py; sync.py will "
                                  "reconcile it. Tell Chris.")
            if not found:
                _update(trade_id, status="failed_submit", exit_reason="submit_failed", date_close=today,
                        error=err[:300])
                raise D.DeskError(f"ORDER NOT PLACED: {symbol}: {err[:300]}")
            oid = found["id"]

    got = confirm(b, oid)
    if not got:
        _update(trade_id, order_id=oid, status="pending")
        raise D.DeskError(f"ORDER NOT CONFIRMED: submitted {oid} but its status could not be read. Do NOT report it as "
                          "placed. sync.py will pick it up.")
    status = got["status"]
    if status in DEAD:
        _update(trade_id, order_id=oid, status="rejected", exit_reason=f"order {status}", date_close=today)
        raise D.DeskError(f"ORDER REJECTED: Alpaca set {symbol} order {oid} to '{status}'. It was NOT placed.")
    filled = status == "filled"
    fields = {"order_id": got["id"], "status": "open" if filled else "pending"}
    if filled:
        fields.update({"entry_fill": float(got["filled_avg_price"]), "date_fill": today,
                       "shares": int(float(got.get("filled_qty") or c["shares"]))})
    _update(trade_id, **fields)
    print(f"PLACED {symbol} {c['direction'].upper()} tier {c['tier']} | {c['shares']} sh | entry {c['entry']} "
          f"stop {c['stop']} target {c['target']} | ${c['risk_dollars']} at risk | order {got['id']} status {status}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", help="reports/order_<SYMBOL>_<date>.json written with your file tool (preferred)")
    ap.add_argument("--symbol")
    ap.add_argument("--thesis")
    ap.add_argument("--invalidation")
    ap.add_argument("--dissent")
    ap.add_argument("--p-target", type=float)
    ap.add_argument("--earnings-checked", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--smoke-test", action="store_true")
    a = ap.parse_args(argv)
    if a.smoke_test:
        return smoke_test()
    if a.input:
        if a.thesis or a.invalidation or a.dissent or a.p_target is not None:
            ap.error("with --input, put everything in the order file (no --thesis/--p-target on the command line)")
        d = load_order_file(a.input)
        sym = str(d.get("symbol") or "").upper()
        if a.symbol and a.symbol.upper() != sym:
            ap.error(f"--symbol {a.symbol} does not match the order file ({sym})")
        ec = d.get("earnings_checked", False)
        if not isinstance(ec, bool):
            raise D.DeskError('REFUSED: "earnings_checked" must be true or false.')
        note = d.get("earnings_note")
        place(sym, d.get("thesis"), d.get("invalidation"), d.get("dissent"), d.get("p_target"), ec or a.earnings_checked,
              a.dry_run, "file", clean_text("earnings_note", note) if note else None)
    else:
        if not a.symbol or a.p_target is None:
            ap.error("use --input reports/order_<SYMBOL>_<date>.json (or the legacy --symbol and --p-target)")
        D.warn("free text passed on the command line can be changed by the shell (for example $105 -> 05). "
               "Use --input with an order file instead.")
        place(a.symbol.upper(), a.thesis, a.invalidation, a.dissent, a.p_target, a.earnings_checked, a.dry_run, "cli")


if __name__ == "__main__":
    main()
