"""Reconcile journal/trades.jsonl with Alpaca. Run any time; run with --close at 15:50 ET.

Usage:
  python scripts/sync.py            # update fills and exits (target/stop hits) from Alpaca
  python scripts/sync.py --close    # same, plus cancel unfilled entries and apply the 10-day time stop

Safety rules this file follows:
- Each trade is handled on its own; one failure never stops the others, and the journal is saved after every trade.
- The time stop only cancels a position's stop/target once Alpaca confirms they're cancelled, and only while the
  market is open. If the market close then fails, the stop and target are put back as an OCO order.
- Anything that needs a human is printed under ALERTS. Socrates must pass those to Chris word for word.
"""
import argparse
import datetime as dt
import json
import re
import time

import broker as BR
import control as C
import desk as D

TERMINAL = {"filled", "canceled", "expired", "rejected", "replaced", "done_for_day", "stopped", "suspended"}
sleep = time.sleep      # tests replace this


def et_date(ts, default=None):
    """Alpaca timestamps carry up to 9 fractional digits; Python < 3.11 only parses 3 or 6. Trim to 6."""
    if not ts:
        return default or D.today_str()
    s = re.sub(r"(\.\d{6})\d+", r"\1", ts.replace("Z", "+00:00"))
    s = re.sub(r"\.(\d{1,5})(?=[+-])", lambda m: "." + m.group(1).ljust(6, "0"), s)
    return dt.datetime.fromisoformat(s).astimezone(D.ET).date().isoformat()


def record_exit(t, price, reason, when):
    t.update({"status": "closed", "exit_price": round(float(price), 4), "exit_reason": reason, "date_close": when})
    t["result_R"] = D.r_multiple(t["direction"], t["entry_fill"], t["stop"], t["exit_price"])
    sign = 1 if t["direction"] == "long" else -1
    t["pnl"] = round(sign * (t["exit_price"] - t["entry_fill"]) * t["shares"], 2)


def exit_side(t):
    return "sell" if t["direction"] == "long" else "buy"


def wait_terminal(b, oid, timeout_s=10.0, step=0.5):
    """Poll until the order (and every leg) is in a final state. Returns the last order seen."""
    o = b.order(oid)
    waited = 0.0
    while waited < timeout_s:
        if o.get("status") in TERMINAL and all(l.get("status") in TERMINAL for l in o.get("legs") or []):
            return o
        sleep(step)
        waited += step
        o = b.order(oid)
    return o


def exit_orders(b, t, o):
    """Every order that can close this position: the bracket's legs plus any OCO we placed later."""
    out = list(o.get("legs") or [])
    if t.get("exit_order_id"):
        e = b.order(t["exit_order_id"])
        out += [e] + list(e.get("legs") or [])
    return out


def filled_exit(orders):
    leg = next((l for l in orders if l.get("status") == "filled"), None)
    if not leg:
        return None
    return leg, ("target" if leg.get("type") == "limit" else "stop")


def protect(b, t, alerts, why):
    """Put the original stop and target back as an exit-only OCO for the shares we hold."""
    try:
        e = b.submit_oco(t["symbol"], t["shares"], exit_side(t), t["target"], t["stop"],
                         client_order_id=f"{t['trade_id']}-oco-{int(time.time())}")
        t["exit_order_id"] = e["id"]
        return True
    except Exception as ex:
        alerts.append(f"{t['symbol']}: POSITION HAS NO STOP. {why}, and re-placing the stop/target failed "
                      f"({type(ex).__name__}: {ex}). Tell Chris immediately; set a stop in the Alpaca dashboard.")
        return False


def cancel_and_wait(b, ids):
    """Cancel every listed order id that is still working, then wait until each is final. Returns final orders."""
    for i in ids:
        try:
            b.cancel_order(i)
        except BR.BrokerError:
            pass     # already final, or Alpaca refused; the wait below decides
    return [wait_terminal(b, i) for i in ids]


def adopt_fill(t, o, partial=False):
    t.update({"entry_fill": float(o["filled_avg_price"]), "date_fill": et_date(o.get("filled_at")), "status": "open",
              "shares": int(float(o.get("filled_qty") or t["shares"]))})
    if partial:
        t["partial_fill"] = True
        t["risk_dollars"] = round(t["shares"] * abs(t["entry_fill"] - t["stop"]), 2)


def handle_partial(b, t, o, notes, alerts):
    """Entry partly filled: cancel the rest, keep the filled shares, and protect them with an OCO."""
    final = cancel_and_wait(b, [o["id"]])[0]
    if final.get("status") not in TERMINAL:
        alerts.append(f"{t['symbol']}: entry partly filled and the rest would not cancel (status {final.get('status')}). Tell Chris.")
        return
    qty = int(float(final.get("filled_qty") or 0))
    if qty <= 0:
        t.update({"status": "unfilled", "date_close": D.today_str(), "exit_reason": "unfilled"})
        notes.append(f"{t['symbol']}: entry never filled, cancelled")
        return
    adopt_fill(t, final, partial=True)
    live_legs = [l["id"] for l in final.get("legs") or [] if l.get("status") not in TERMINAL]
    cancel_and_wait(b, live_legs)
    protect(b, t, alerts, "Partial fill")
    notes.append(f"{t['symbol']}: entry PARTIALLY filled ({qty} of {t.get('planned_shares', '?')} sh at {t['entry_fill']}); "
                 f"rest cancelled, stop {t['stop']} / target {t['target']} placed for {qty} sh")


def time_stop(b, t, o, notes, alerts):
    live = [x["id"] for x in exit_orders(b, t, o) if x.get("status") not in TERMINAL and x.get("id")]
    final = cancel_and_wait(b, live)
    done = filled_exit(exit_orders(b, t, b.order(t["order_id"])))
    if done:        # the stop or target filled while we were cancelling: that is the exit
        leg, reason = done
        record_exit(t, leg["filled_avg_price"], reason, et_date(leg.get("filled_at")))
        notes.append(f"{t['symbol']}: {reason} hit at {t['exit_price']} ({t['result_R']}R) during the time stop")
        return
    if any(x.get("status") not in TERMINAL for x in final):
        t["time_stop_due"] = True
        alerts.append(f"{t['symbol']}: time stop due but its stop/target would not cancel. Position untouched (still "
                      "protected); retried at the next CLOSE run.")
        return
    try:
        c = b.close_position(t["symbol"])
    except BR.NotFound:
        alerts.append(f"RECONCILE {t['symbol']}: time stop due but Alpaca shows no position. Check manually.")
        t["time_stop_due"] = True
        return
    except Exception as e:
        t["time_stop_due"] = True
        if protect(b, t, alerts, f"Time-stop market close failed ({type(e).__name__})"):
            alerts.append(f"{t['symbol']}: TIME STOP FAILED ({type(e).__name__}: {e}). Stop {t['stop']} and target "
                          f"{t['target']} were put back; the close is retried at the next CLOSE run.")
        return
    t.update({"status": "closing", "close_order_id": c["id"], "time_stop_due": False})
    c2 = wait_terminal(b, c["id"], timeout_s=5.0)
    if c2.get("status") == "filled":
        record_exit(t, c2["filled_avg_price"], "time", et_date(c2.get("filled_at")))
        notes.append(f"{t['symbol']}: 10-day time stop, closed at {t['exit_price']} ({t['result_R']}R)")
    else:
        notes.append(f"{t['symbol']}: 10-day time stop, close order sent ({c['id']}); fill recorded on next sync")


def handle(b, t, positions, today, close_mode, market_open, notes, alerts):
    if t["status"] == "submitting":        # place.py died between logging and confirming
        found = b.order_by_client_id(t["trade_id"])
        if not found:
            t.update({"status": "failed_submit", "exit_reason": "submit_failed", "date_close": today})
            notes.append(f"{t['symbol']}: order was never received by Alpaca; marked failed_submit")
            return
        t.update({"order_id": found["id"], "status": "pending"})
        notes.append(f"{t['symbol']}: recovered order {found['id']} after an interrupted placement")

    o = b.order(t["order_id"])

    if t["status"] == "closing":
        c = b.order(t["close_order_id"])
        st = c.get("status")
        if st == "filled":
            record_exit(t, c["filled_avg_price"], "time", et_date(c.get("filled_at")))
            notes.append(f"{t['symbol']}: time-stop exit filled at {t['exit_price']} ({t['result_R']}R)")
        elif st in TERMINAL:
            t.update({"status": "open", "time_stop_due": True, "close_order_id": None})
            if t["symbol"] in positions:
                protect(b, t, alerts, f"Time-stop close order ended '{st}'")
                alerts.append(f"{t['symbol']}: time-stop close order ended '{st}'. Stop/target put back; the close is "
                              "retried at the next CLOSE run.")
            else:
                alerts.append(f"RECONCILE {t['symbol']}: close order ended '{st}' and Alpaca shows no position. Check manually.")
        else:
            notes.append(f"{t['symbol']}: time-stop close order still working ({st})")
        return

    if t["status"] == "pending":
        st = o.get("status")
        filled_qty = int(float(o.get("filled_qty") or 0))
        if st == "filled":
            adopt_fill(t, o)
            notes.append(f"{t['symbol']}: entry filled at {t['entry_fill']}")
        elif st in TERMINAL:
            if filled_qty > 0:
                handle_partial(b, t, o, notes, alerts)
            else:
                t.update({"status": "unfilled", "date_close": today, "exit_reason": st})
                notes.append(f"{t['symbol']}: entry {st}, marked unfilled")
            return
        elif not close_mode:
            if st == "partially_filled":
                notes.append(f"{t['symbol']}: entry partly filled ({filled_qty} sh); settled at the CLOSE run")
            return
        elif st == "partially_filled":
            handle_partial(b, t, o, notes, alerts)
            return
        else:
            final = cancel_and_wait(b, [o["id"]])[0]
            if final.get("status") == "filled":
                adopt_fill(t, final)
                notes.append(f"{t['symbol']}: entry filled at {t['entry_fill']} just before the cancel")
            elif int(float(final.get("filled_qty") or 0)) > 0:
                handle_partial(b, t, final, notes, alerts)
                return
            elif final.get("status") in TERMINAL:
                t.update({"status": "unfilled", "date_close": today, "exit_reason": "unfilled"})
                notes.append(f"{t['symbol']}: entry never filled, cancelled")
                return
            else:
                alerts.append(f"{t['symbol']}: unfilled entry would not cancel (status {final.get('status')}). Tell Chris.")
                return

    # open position: look for an exit
    o = b.order(t["order_id"])
    done = filled_exit(exit_orders(b, t, o))
    if done:
        leg, reason = done
        record_exit(t, leg["filled_avg_price"], reason, et_date(leg.get("filled_at")))
        notes.append(f"{t['symbol']}: {reason} hit at {t['exit_price']} ({t['result_R']}R, ${t['pnl']})")
        return
    if t["symbol"] not in positions:
        alerts.append(f"RECONCILE {t['symbol']}: journal says open but Alpaca shows no position and no exit fill. Check manually.")
        return
    if not close_mode:
        return
    try:
        held = D.trading_days_between(b, t["date_fill"], today)
    except Exception as e:
        alerts.append(f"{t['symbol']}: could not count days held ({type(e).__name__}); the 10-day time stop was NOT "
                      "checked today. Tell Chris.")
        return
    t["days_held"] = held
    if held >= D.TIME_STOP_DAYS or t.get("time_stop_due"):
        if not market_open:
            t["time_stop_due"] = True
            notes.append(f"{t['symbol']}: 10-day time stop due, but the market is closed right now. Stop/target left in "
                         "place; the close runs at the next CLOSE.")
            return
        time_stop(b, t, o, notes, alerts)


def run(close_mode=False):
    with D.journal_lock():
        return _run(close_mode)


def _run(close_mode):
    b = D.broker()
    today = D.today_str()
    trades = D.read_jsonl(D.TRADES)
    positions = {p["symbol"]: p for p in b.positions()}
    notes, alerts = [], []

    market_open = False
    if close_mode:
        try:
            trading_day, market_open, sess_close = D.market_status(b)
            if not trading_day:
                notes.append(f"{today} is not a trading day: entries are cancelled if any, time stops wait.")
            elif not market_open:
                notes.append(f"Market already closed today (session close {sess_close} ET): time stops wait for the next CLOSE run.")
        except Exception as e:
            alerts.append(f"Market clock unavailable ({type(e).__name__}); no time stops today. Tell Chris.")

    for t in trades:
        if t.get("status") not in D.ACTIVE:
            continue
        before = json.dumps(t, sort_keys=True)
        try:
            handle(b, t, positions, today, close_mode, market_open, notes, alerts)
        except Exception as e:
            alerts.append(f"{t['symbol']}: sync error ({type(e).__name__}: {e}); left as '{t['status']}'. Tell Chris.")
        if json.dumps(t, sort_keys=True) != before:
            D.write_jsonl(D.TRADES, trades)     # save after every trade: a later failure can't lose this one

    print(f"SYNC {today}{' (CLOSE)' if close_mode else ''}")
    if alerts:
        print("ALERTS (tell Chris, word for word):")
        print("\n".join(f"  {a}" for a in alerts))
    print("\n".join(notes) if notes else "No changes.")
    print("OPEN POSITIONS:")
    any_open = False
    for t in trades:
        if t.get("status") in ("open", "closing"):
            any_open = True
            p = positions.get(t["symbol"])
            px = float(p["current_price"]) if p else None
            r = D.r_multiple(t["direction"], t["entry_fill"], t["stop"], px) if px and t.get("entry_fill") else None
            tag = " CLOSING" if t["status"] == "closing" else (" TIME STOP DUE" if t.get("time_stop_due") else "")
            print(f"  {t['symbol']} {t['direction']} tier {t['tier']}{tag} | {t['shares']} sh | entry {t['entry_fill']} now {px} | "
                  f"{r}R unrealized | days held {t.get('days_held', '?')}")
    if not any_open:
        print("  none")
    try:
        done = C.resolve(b, today)
        if done:
            print(f"CONTROL: resolved {len(done)} random control entries (logged to journal/control.jsonl)")
    except Exception as e:      # never let the control group break the real bookkeeping
        print(f"CONTROL: could not update controls ({type(e).__name__}: {e})")
    closed_today = [t for t in trades if t.get("status") == "closed" and t.get("date_close") == today]
    if closed_today:
        print("CLOSED TODAY (write a one-sentence lesson for each):")
        for t in closed_today:
            print(f"  {t['trade_id']}: {t['exit_reason']} {t['result_R']}R ${t['pnl']}")
    return {"alerts": alerts, "notes": notes}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--close", action="store_true")
    run(ap.parse_args().close)
