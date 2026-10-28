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
import hashlib
import json
import math
import re
import sys
import time
from pathlib import Path

import broker as BR
import desk as D

LIVE = {"new", "accepted", "pending_new", "accepted_for_bidding", "held", "partially_filled", "filled",
        "pending_replace", "replaced", "calculated", "done_for_day"}
DEAD = {"rejected", "canceled", "expired", "suspended"}
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
sleep = time.sleep      # tests replace this


def confirm(b, order_id, tries=5, timeout_s=20.0):
    """The order's status once Alpaca reports one, or None (bounded by wall-clock time, not just attempts)."""
    deadline = time.monotonic() + timeout_s
    for _ in range(tries):
        try:
            o = b.order(order_id)
            if o.get("status"):
                return o
        except Exception:
            pass
        if time.monotonic() >= deadline:
            break
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
    if len(val) > D.TEXT_MAX * 2:
        raise D.DeskError(f"REFUSED: {name} is {len(val)} characters; keep it under {D.TEXT_MAX}.")
    val = D.plain_text(val, limit=10 ** 6)
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
    extra = set(data) - {"symbol", "p_target", "thesis", "invalidation", "dissent", "earnings_checked", "earnings_note",
                         "next_earnings"}
    if extra:
        raise D.DeskError(f"REFUSED: unknown keys in order file: {', '.join(sorted(extra))}.")
    return data


def verify_plan(pj, plan_text, c, b, today, recheck_limits=True):
    """The order must be exactly what score.py planned in its LATEST run today (plan fingerprint and the plan's own
    candidate row in the journal), and it must still fit every risk limit against live positions right now. So a
    stale, restored, edited or forged plan can never place an order the rules wouldn't."""
    cands = [r for r in D.read_jsonl(D.CANDIDATES) if r.get("date") == today and str(r.get("run", "")).startswith("decision")]
    if not cands:
        raise D.DeskError("REFUSED: no DECISION run is logged for today. Nothing sent. Tell Chris.")
    def run_n(r):
        m = re.search(r"rerun(\d+)$", str(r.get("run", "")))
        return int(m.group(1)) if m else 0
    latest = max(run_n(r) for r in cands)
    latest_rows = [r for r in cands if run_n(r) == latest]
    sha = hashlib.sha256(plan_text.encode()).hexdigest()
    if pj.get("run", "decision") != latest_rows[0].get("run") or sha not in {r.get("plan_sha") for r in latest_rows}:
        raise D.DeskError("REFUSED: plan file is not the plan from today's latest DECISION run (it was edited, replaced "
                          "or restored). Nothing sent. Tell Chris.")
    row = next((r for r in latest_rows if r.get("symbol") == c["symbol"]), None)
    if not row or row.get("action") != "planned":
        raise D.DeskError(f"REFUSED: the journal does not show {c['symbol']} as planned in today's DECISION run.")
    for k in ("direction", "tier", "entry", "stop", "target", "shares"):
        if row.get(k) != c.get(k):
            raise D.DeskError(f"REFUSED: plan and journal disagree on {k} for {c['symbol']}. Nothing sent. Tell Chris.")

    def num(k):
        v = c.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
            raise D.DeskError(f"REFUSED: plan value {k}={v!r} is not a positive number.")
        return float(v)
    entry, stop, target = num("entry"), num("stop"), num("target")
    shares = c.get("shares")
    if c["symbol"] not in D.UNIVERSE or c.get("direction") not in ("long", "short") or c.get("tier") not in ("A", "B"):
        raise D.DeskError("REFUSED: plan entry is outside the universe / direction / tier rules.")
    if isinstance(shares, bool) or not isinstance(shares, int) or shares < 1:
        raise D.DeskError(f"REFUSED: plan share count {shares!r} is invalid.")
    if not ((c["direction"] == "long" and stop < entry < target) or (c["direction"] == "short" and target < entry < stop)):
        raise D.DeskError("REFUSED: stop/target are on the wrong side of the entry.")
    if not rr_matches(entry, stop, target, c.get("rr")):
        raise D.DeskError(f"REFUSED: R:R doesn't match the plan ({c.get('rr')}) or is below {D.MIN_RR}.")
    equity = float(b.account()["equity"])
    plan_eq = float(c.get("equity_at_plan") or equity)
    if abs(plan_eq - equity) / equity > 0.05:
        raise D.DeskError("REFUSED: account equity moved more than 5% since the plan was made. Tell Chris.")
    if shares > D.size(plan_eq, D.RISK_PCT[c["tier"]], entry, stop):
        raise D.DeskError("REFUSED: the plan's share count is larger than the risk rules allow.")

    # the risk limits again, against what is live right now (earlier orders from the same plan included).
    # Skipped only when finishing an interrupted placement of this same order, which already passed them.
    risk = round(shares * abs(entry - stop), 2)
    c["risk_dollars"] = risk
    if not recheck_limits:
        return
    positions = b.positions()
    orders = b.open_orders()
    jopen = D.open_trades()
    tracked = {t["symbol"] for t in jopen}
    busy = {p["symbol"] for p in positions} | {o["symbol"] for o in orders} | tracked
    if c["symbol"] in busy:
        raise D.DeskError(f"REFUSED: risk rule 5: already have a position or order in {c['symbol']}.")
    if len(busy) >= D.MAX_POSITIONS:
        raise D.DeskError("REFUSED: risk rule 2: already at the 5-position limit.")
    open_risk = sum(float(t.get("risk_dollars") or 0) for t in jopen)
    open_risk += sum(equity * D.RISK_PCT["A"] for s in ({p["symbol"] for p in positions} | {o["symbol"] for o in orders})
                     if s not in tracked)
    if open_risk + risk > equity * D.MAX_OPEN_RISK_PCT + 0.01:
        raise D.DeskError("REFUSED: risk rule 3: total open risk would exceed 2.5%.")
    theme = D.THEME_OF.get(c["symbol"])
    if sum(1 for s in busy if D.THEME_OF.get(s) == theme) >= D.MAX_PER_THEME:
        raise D.DeskError(f"REFUSED: risk rule 4: already 2 positions in theme '{theme}'.")
    c["risk_dollars"] = risk
    px = b.latest_prices([c["symbol"]]).get(c["symbol"], {})
    ref = px.get("last") or ((px.get("bid", 0) + px.get("ask", 0)) / 2)
    if ref and abs(ref - entry) / entry > 0.03:
        raise D.DeskError(f"REFUSED: {c['symbol']} is trading at {ref}, more than 3% from the planned entry {entry}. "
                          "Nothing sent; report it as a skipped order.")


def rr_matches(entry, stop, target, rr_plan):
    """The plan's rr was computed before stop/target were rounded to cents, so allow exactly that much rounding."""
    try:
        rr_plan = float(rr_plan)
    except (TypeError, ValueError):
        return False
    rr = abs(target - entry) / abs(entry - stop)
    tol = 0.005 * (1 + rr) / abs(entry - stop) + 0.006
    return rr_plan >= D.MIN_RR and abs(rr - rr_plan) <= tol


def check_next_earnings(b, today, next_earnings):
    """A hand-checked earnings date must be a real date beyond the 10-trading-day blackout window."""
    if not isinstance(next_earnings, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", next_earnings or ""):
        raise D.DeskError('REFUSED: "earnings_checked" needs "next_earnings": "YYYY-MM-DD" (the date you found) and '
                          '"earnings_note" with the source.')
    if next_earnings < today:
        raise D.DeskError(f"REFUSED: next_earnings {next_earnings} is in the past; give the NEXT report date.")
    try:
        window_end = D.trading_days_ahead(b, today, 10)
    except Exception as e:
        raise D.DeskError(f"MARKET CALENDAR UNAVAILABLE ({type(e).__name__}); can't check the earnings date. Tell Chris.")
    if next_earnings <= window_end:
        raise D.DeskError(f"REFUSED: earnings on {next_earnings} is inside the 10-trading-day blackout (through "
                          f"{window_end}). Do not place it; report why.")


def _update(trade_id, **fields):
    rows = D.read_jsonl(D.TRADES)
    for r in rows:
        if r.get("trade_id") == trade_id:
            r.update(fields)
    D.write_jsonl(D.TRADES, rows)


def place(symbol, thesis, invalidation, dissent, p_target, earnings_checked=False, dry_run=False, text_source="file",
          earnings_note=None, next_earnings=None):
    with D.journal_lock():
        return _place(symbol, thesis, invalidation, dissent, p_target, earnings_checked, dry_run, text_source,
                      earnings_note, next_earnings)


def _place(symbol, thesis, invalidation, dissent, p_target, earnings_checked, dry_run, text_source, earnings_note,
           next_earnings):
    today = D.today_str()
    rv = D.rules_version()
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z]{1,5}", symbol or ""):
        raise D.DeskError("REFUSED: symbol must be 1-5 capital letters.")
    plan_file = D.REPORTS / f"plan_{today}.json"
    if not plan_file.exists():
        raise D.DeskError(f"REFUSED: no plan for today ({plan_file.name}). Run score.py --mode decision first.")
    plan_text = plan_file.read_text()
    try:
        pj = json.loads(plan_text)
    except ValueError:
        raise D.DeskError("REFUSED: today's plan file is not valid JSON. Tell Chris.")
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
        raise D.DeskError(f"REFUSED: {symbol} earnings date unverified. Look it up; if it is more than 10 trading days "
                          'away, set "earnings_checked": true, "next_earnings": "YYYY-MM-DD" and "earnings_note": "<source>" '
                          "in the order file.")

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
    verify_plan(pj, plan_text, c, b, today, recheck_limits=not retry_submitting)
    if c.get("earnings_unverified"):
        check_next_earnings(b, today, next_earnings)

    if not retry_submitting:
        D.append_jsonl(D.TRADES, [{
            "trade_id": trade_id, "order_id": None, "rules_version": rv, "date_open": today,
            "symbol": symbol, "direction": c["direction"], "tier": c["tier"], "quota": c.get("quota", False),
            "score": c["score"], "breakdown": c["breakdown"], "planned_entry": c["entry"], "entry_fill": None,
            "date_fill": None, "stop": c["stop"], "target": c["target"], "shares": c["shares"],
            "planned_shares": c["shares"], "risk_dollars": c["risk_dollars"],
            "equity_at_open": c.get("equity_at_plan"), "p_target": p_target, "p_base": round(base, 3),
            "thesis": thesis, "invalidation": invalidation, "dissent": dissent, "text_source": text_source,
            "earnings_checked": bool(earnings_checked), "earnings_note": earnings_note, "next_earnings": next_earnings,
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
                found = None
            if not found:
                if e.definitely_not_accepted:      # Alpaca answered with a clear refusal: nothing exists
                    _update(trade_id, status="failed_submit", exit_reason="submit_failed", date_close=today,
                            error=err[:300])
                    raise D.DeskError(f"ORDER NOT PLACED: {symbol}: {err[:300]}")
                # timeout / 5xx: Alpaca may still have it. Leave the row as 'submitting' for sync.py to settle.
                raise D.DeskError(f"ORDER STATUS UNKNOWN for {symbol} ({err[:150]}). Do NOT report it as placed and "
                                  "do not rerun place.py; sync.py will find out what happened. Tell Chris.")
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
              a.dry_run, "file", clean_text("earnings_note", note) if note else None, d.get("next_earnings"))
    else:
        if not a.symbol or a.p_target is None:
            ap.error("use --input reports/order_<SYMBOL>_<date>.json (or the legacy --symbol and --p-target)")
        D.warn("free text passed on the command line can be changed by the shell (for example $105 -> 05). "
               "Use --input with an order file instead.")
        place(a.symbol.upper(), a.thesis, a.invalidation, a.dissent, a.p_target, a.earnings_checked, a.dry_run, "cli")


if __name__ == "__main__":
    main()
