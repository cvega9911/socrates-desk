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

TERMINAL = {"filled", "canceled", "expired", "rejected", "suspended"}
# "replaced" means someone edited the order in the dashboard and it lives on under a new id (replaced_by). Exit orders
# are always followed to their replacement; a replaced order is never counted as protecting anything by itself.
STOP_TYPES = {"stop", "stop_limit", "trailing_stop"}
sleep = time.sleep      # tests replace this


def et_date(ts, default=None):
    """Alpaca timestamps carry up to 9 fractional digits; Python < 3.11 only parses 3 or 6. Trim to 6."""
    if not ts:
        return default or D.today_str()
    s = re.sub(r"(\.\d{6})\d+", r"\1", ts.replace("Z", "+00:00"))
    s = re.sub(r"\.(\d{1,5})(?=[+-])", lambda m: "." + m.group(1).ljust(6, "0"), s)
    return dt.datetime.fromisoformat(s).astimezone(D.ET).date().isoformat()


def record_exit(t, price, reason, when, qty=None):
    """qty: shares the exit order actually filled, when known (it can be fewer than the trade's shares after a
    re-protect sized to what was still held)."""
    t.update({"status": "closed", "exit_price": round(float(price), 4), "exit_reason": reason, "date_close": when})
    t["result_R"] = D.r_multiple(t["direction"], t["entry_fill"], t["stop"], t["exit_price"])
    sign = 1 if t["direction"] == "long" else -1
    try:
        q = int(float(qty)) if qty else 0
    except (TypeError, ValueError):
        q = 0
    q = q or int(t.get("protected_qty") or t["shares"])
    if q != t["shares"]:
        t["exit_qty"] = q
    t["pnl"] = round(sign * (t["exit_price"] - t["entry_fill"]) * q, 2)


def exit_side(t):
    return "sell" if t["direction"] == "long" else "buy"


def wait_terminal(b, oid, timeout_s=10.0, step=0.5):
    """Poll until the order (and every leg) is in a final state, or the wall-clock deadline passes (slow network
    calls count against it too). Returns the last order seen."""
    deadline = time.monotonic() + timeout_s
    o = b.order(oid)
    polls = 0
    while time.monotonic() < deadline and polls < timeout_s / step:
        if o.get("status") in TERMINAL and all(l.get("status") in TERMINAL | {"replaced"} for l in o.get("legs") or []):
            return o
        sleep(step)
        polls += 1
        o = b.order(oid)
    return o


def exit_ids(t):
    ids = list(t.get("exit_order_ids") or [])
    if t.get("exit_order_id") and t["exit_order_id"] not in ids:      # rows written before exit_order_ids existed
        ids.insert(0, t["exit_order_id"])
    return ids


def is_live(x):
    return x.get("status") not in TERMINAL and x.get("status") != "replaced"


def follow(b, x):
    """An order edited in the dashboard is 'replaced' and lives on under replaced_by: return the current version.
    If the chain can't be followed, the 'replaced' order comes back as is and counts as not live."""
    for _ in range(10):
        if x.get("status") != "replaced" or not x.get("replaced_by"):
            return x
        x = b.order(x["replaced_by"])
    return x


def exit_orders(b, t, o):
    """Every order that can close this position: the bracket's legs plus every OCO we ever placed for it, each
    followed to its current version if it was edited by hand."""
    out = [follow(b, l) for l in o.get("legs") or []]
    for i in exit_ids(t):
        e = follow(b, b.order(i))
        out += [e] + [follow(b, l) for l in e.get("legs") or []]
    return out


def has_live_stop(orders):
    return any(is_live(x) and x.get("type") in STOP_TYPES for x in orders)


def filled_exit(orders):
    leg = next((l for l in orders if l.get("status") == "filled"), None)
    if not leg:
        return None
    return leg, ("target" if leg.get("type") == "limit" else "stop")


def held_qty(b, t):
    """Shares Alpaca says we hold in this symbol right now (fresh read, not the start-of-run snapshot), counted only
    if the position is on our side (a long trade needs a long position)."""
    p = next((p for p in b.positions() if p["symbol"] == t["symbol"]), None)
    if not p:
        return 0
    q = int(float(p.get("qty", t["shares"] if t["direction"] == "long" else -t["shares"])))
    if (t["direction"] == "long" and q <= 0) or (t["direction"] == "short" and q >= 0):
        return 0
    return abs(q)


def find_close_fill(b, t, today):
    """After a close whose reply was lost: the filled exit order for this symbol today, if there is one."""
    side = exit_side(t)
    for o in b.recent_orders(t["symbol"], today):
        if o.get("status") == "filled" and o.get("side") == side and o.get("id") not in (t.get("order_id"),):
            return o
    return None


def protect(b, t, alerts, why, qty=None):
    """Put the original stop and target back as an exit-only OCO for the shares actually held.
    Idempotent: each attempt has a numbered client id, so a submit whose reply was lost is found and reused on the
    next try instead of stacking a second OCO on top."""
    ids = exit_ids(t)
    if qty is None:
        try:
            qty = held_qty(b, t)
        except Exception:
            qty = t["shares"] if t["direction"] == "long" else None   # a stray sell is rejected; a stray buy is not
    if not qty:
        alerts.append(f"{t['symbol']}: {why}, but no matching position could be confirmed, so no new stop was sent. "
                      "Check the position in Alpaca now. Tell Chris.")
        return False
    def no_stop(detail):
        alerts.append(f"{t['symbol']}: POSITION HAS NO STOP. {why}, and re-placing the stop/target failed ({detail}). "
                      "Tell Chris immediately; set a stop in the Alpaca dashboard.")
        return False

    # Client ids are never reused (Alpaca refuses a reused one). Walk past every number already taken: a live one is
    # an OCO whose reply was lost (reuse it); a finished one is recorded and skipped.
    n, e = len(ids) + 1, None
    try:
        for _ in range(20):
            cid = f"{t['trade_id']}-oco{n}"
            found = b.order_by_client_id(cid)
            if not found:
                break
            found = follow(b, found)
            if found.get("status") == "filled":
                alerts.append(f"RECONCILE {t['symbol']}: found an exit order ({cid}) that already filled but was never "
                              "journaled. Check the position in Alpaca now. Tell Chris.")
                return False
            if found["id"] not in ids:
                ids.append(found["id"])
            if is_live(found):
                e = found
                break
            n += 1
        else:
            return no_stop("too many earlier stop orders to check")
    except Exception as ex:
        t["exit_order_ids"] = ids
        return no_stop(f"{type(ex).__name__}: {ex}")
    if e is None:
        try:
            e = b.submit_oco(t["symbol"], qty, exit_side(t), t["target"], t["stop"], client_order_id=cid)
            e = b.order(e["id"]) if not e.get("status") else e
        except Exception as ex:
            try:
                e = b.order_by_client_id(cid)
            except Exception:
                e = None
            if not e or not is_live(e):
                if e:
                    ids.append(e["id"])
                t["exit_order_ids"] = ids
                return no_stop(f"{type(ex).__name__}: {ex}")
        if not is_live(e):
            t["exit_order_ids"] = ids + [e["id"]]
            return no_stop(f"Alpaca says the new order is {e.get('status')}")
        if e["id"] not in ids:
            ids.append(e["id"])
    t["exit_order_ids"] = ids
    t["exit_order_id"] = e["id"]
    if qty != t["shares"]:
        t["protected_qty"] = qty
    return True


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
    legs_now = [follow(b, l) for l in final.get("legs") or []]
    live_legs = [l["id"] for l in legs_now if is_live(l)]
    legs_after = cancel_and_wait(b, live_legs)
    done = filled_exit(legs_after + legs_now)
    if done:            # a bracket leg filled for the partial shares while we were tidying up: that is the exit
        leg, reason = done
        record_exit(t, leg["filled_avg_price"], reason, et_date(leg.get("filled_at")), qty=leg.get("filled_qty"))
        notes.append(f"{t['symbol']}: partial fill of {qty} sh, then {reason} hit at {t['exit_price']} ({t['result_R']}R)")
        return
    if any(is_live(x) for x in legs_after):
        alerts.append(f"{t['symbol']}: entry partly filled ({qty} sh) and its old stop/target would not cancel, so no "
                      "new stop was added (two exit sets could oversell). Check the orders in Alpaca. Tell Chris.")
        return
    placed = protect(b, t, alerts, "Partial fill", qty=qty)
    notes.append(f"{t['symbol']}: entry PARTIALLY filled ({qty} of {t.get('planned_shares', '?')} sh at {t['entry_fill']}); "
                 "rest cancelled, " + (f"stop {t['stop']} / target {t['target']} placed for {qty} sh" if placed
                                       else "but the new stop/target could NOT be placed (see ALERTS)"))


def time_stop(b, t, o, today, notes, alerts):
    """Cancel the stop/target, confirm they're gone, then market-close. Whatever goes wrong after the cancel, the
    position ends up either closed or protected again, never left without a stop."""
    live = [x["id"] for x in exit_orders(b, t, o) if is_live(x) and x.get("id")]
    cancel_and_wait(b, live)
    try:
        after = exit_orders(b, t, b.order(t["order_id"]))
        done = filled_exit(after)
        if done:        # the stop or target filled while we were cancelling: that is the exit
            leg, reason = done
            record_exit(t, leg["filled_avg_price"], reason, et_date(leg.get("filled_at")), qty=leg.get("filled_qty"))
            notes.append(f"{t['symbol']}: {reason} hit at {t['exit_price']} ({t['result_R']}R) during the time stop")
            return
        if any(is_live(x) for x in after):
            t["time_stop_due"] = True
            if has_live_stop(after):
                alerts.append(f"{t['symbol']}: time stop due but some of its exit orders would not cancel. The stop is "
                              "still live (the target may be gone); retried at the next CLOSE run. Tell Chris.")
            else:
                alerts.append(f"{t['symbol']}: POSITION HAS NO STOP. Time stop due, the stop was cancelled but the target "
                              "would not cancel, so the position was not closed. Set a stop in the Alpaca dashboard now. "
                              "Tell Chris immediately.")
            return
        if held_qty(b, t) == 0:
            raise BR.NotFound("no position")
        c = b.close_position(t["symbol"])
    except Exception as e:
        t["time_stop_due"] = True
        _recover_close(b, t, today, e, notes, alerts)
        return
    t.update({"status": "closing", "close_order_id": c["id"], "time_stop_due": False})
    c2 = wait_terminal(b, c["id"], timeout_s=5.0)
    if c2.get("status") == "filled":
        record_exit(t, c2["filled_avg_price"], "time", et_date(c2.get("filled_at")), qty=c2.get("filled_qty"))
        notes.append(f"{t['symbol']}: 10-day time stop, closed at {t['exit_price']} ({t['result_R']}R)")
    elif c2.get("status") in TERMINAL:
        t.update({"status": "open", "close_order_id": None, "time_stop_due": True})
        if protect(b, t, alerts, f"Time-stop close order ended '{c2.get('status')}'"):
            alerts.append(f"{t['symbol']}: time-stop close order ended '{c2.get('status')}'. Stop/target put back; the "
                          "close is retried at the next CLOSE run.")
    else:
        notes.append(f"{t['symbol']}: 10-day time stop, close order sent ({c['id']}); fill recorded on next sync")


def _recover_close(b, t, today, err, notes, alerts):
    """The time stop failed after the stop/target were cancelled. Find out what actually happened."""
    try:
        qty = held_qty(b, t)
    except Exception:
        qty = None      # protect() below decides what is safe without a position read
    if qty == 0:        # the position is gone: maybe the close went through and only the reply was lost
        try:
            f = find_close_fill(b, t, today)
        except Exception:
            f = None
        if f:
            record_exit(t, f["filled_avg_price"], "time", et_date(f.get("filled_at")), qty=f.get("filled_qty"))
            notes.append(f"{t['symbol']}: 10-day time stop, closed at {t['exit_price']} ({t['result_R']}R) "
                         "(confirmed from Alpaca's order history)")
        else:
            alerts.append(f"RECONCILE {t['symbol']}: time stop interrupted ({type(err).__name__}) and Alpaca shows no "
                          "position and no closing fill today. Check manually.")
        return
    if protect(b, t, alerts, f"Time stop interrupted ({type(err).__name__})", qty=qty):
        alerts.append(f"{t['symbol']}: TIME STOP FAILED ({type(err).__name__}: {err}). Stop {t['stop']} and target "
                      f"{t['target']} were put back; the close is retried at the next CLOSE run.")


def handle(b, t, positions, today, close_mode, market_open, notes, alerts):
    if t["status"] == "submitting":        # place.py died or timed out between logging and confirming
        found = b.order_by_client_id(t["trade_id"])
        if not found:
            if (t.get("date_open") or today) < today or close_mode:
                t.update({"status": "failed_submit", "exit_reason": "submit_failed", "date_close": today})
                notes.append(f"{t['symbol']}: order was never received by Alpaca; marked failed_submit")
            else:
                notes.append(f"{t['symbol']}: placement outcome still unknown (Alpaca has no record yet); checked again later")
            return
        t.update({"order_id": found["id"], "status": "pending"})
        notes.append(f"{t['symbol']}: recovered order {found['id']} after an interrupted placement")

    o = b.order(t["order_id"])

    if t["status"] == "closing":
        c = b.order(t["close_order_id"])
        st = c.get("status")
        if st == "filled":
            record_exit(t, c["filled_avg_price"], "time", et_date(c.get("filled_at")), qty=c.get("filled_qty"))
            notes.append(f"{t['symbol']}: time-stop exit filled at {t['exit_price']} ({t['result_R']}R)")
        elif st in TERMINAL:
            t.update({"status": "open", "time_stop_due": True, "close_order_id": None})
            if held_qty(b, t):
                if protect(b, t, alerts, f"Time-stop close order ended '{st}'"):
                    alerts.append(f"{t['symbol']}: time-stop close order ended '{st}'. Stop/target put back; the close "
                                  "is retried at the next CLOSE run.")
            else:
                alerts.append(f"RECONCILE {t['symbol']}: close order ended '{st}' and Alpaca shows no position. Check manually.")
        else:
            notes.append(f"{t['symbol']}: time-stop close order still working ({st})")
        return

    if t["status"] == "pending":
        st = o.get("status")
        filled_qty = int(float(o.get("filled_qty") or 0))
        stale = (t.get("date_open") or today) < today     # entries are day orders in spirit: never let one live on
        if st == "filled":
            adopt_fill(t, o)
            notes.append(f"{t['symbol']}: entry filled at {t['entry_fill']}")
        elif st in TERMINAL:
            if filled_qty > 0:
                handle_partial(b, t, o, notes, alerts)
            elif st == "rejected":
                t.update({"status": "rejected", "date_close": today, "exit_reason": "order rejected"})
                alerts.append(f"{t['symbol']}: Alpaca rejected the entry order after it was placed. It never traded.")
            else:
                t.update({"status": "unfilled", "date_close": today, "exit_reason": st})
                notes.append(f"{t['symbol']}: entry {st}, marked unfilled")
            return
        elif st == "replaced":
            alerts.append(f"{t['symbol']}: the entry order was edited by hand in Alpaca (status replaced). The desk "
                          "won't guess what replaced it. Tell Chris.")
            return
        elif st == "partially_filled":
            # Alpaca's bracket legs don't protect a partial entry, so settle it right away: keep the filled
            # shares, cancel the rest, and give the filled shares their own stop/target.
            handle_partial(b, t, o, notes, alerts)
            return
        elif not (close_mode or stale):
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
        record_exit(t, leg["filled_avg_price"], reason, et_date(leg.get("filled_at")), qty=leg.get("filled_qty"))
        notes.append(f"{t['symbol']}: {reason} hit at {t['exit_price']} ({t['result_R']}R, ${t['pnl']})")
        return
    if t["symbol"] not in positions:
        positions.update({p["symbol"]: p for p in b.positions()})      # it may have filled after our first read
    if t["symbol"] not in positions:
        alerts.append(f"RECONCILE {t['symbol']}: journal says open but Alpaca shows no position and no exit fill. Check manually.")
        return
    # Every held position must have a live stop. If its bracket legs or OCO died (cancelled by hand, rejected,
    # expired), put the stop and target back now instead of waiting for someone to notice.
    # A live target alone doesn't count: if the stop is gone, the leftovers are cancelled and a fresh OCO goes in.
    exits = exit_orders(b, t, o)
    if not has_live_stop(exits) and not (close_mode and t.get("time_stop_due") and market_open):
        leftovers = [x["id"] for x in exits if is_live(x) and x.get("id")]
        if leftovers:
            cancel_and_wait(b, leftovers)
        o = b.order(t["order_id"])
        exits = exit_orders(b, t, o)        # fresh read: an exit may have filled since the check above
        done = filled_exit(exits)
        if done:
            leg, reason = done
            record_exit(t, leg["filled_avg_price"], reason, et_date(leg.get("filled_at")), qty=leg.get("filled_qty"))
            notes.append(f"{t['symbol']}: {reason} hit at {t['exit_price']} ({t['result_R']}R, ${t['pnl']})")
            return
        if any(is_live(x) for x in exits):
            alerts.append(f"{t['symbol']}: POSITION HAS NO STOP. Its stop is gone and the leftover target would not "
                          "cancel, so no new stop was added (two exit sets could oversell). Set a stop in the Alpaca "
                          "dashboard now. Tell Chris immediately.")
            return
        qty = held_qty(b, t)
        if qty == 0:
            alerts.append(f"RECONCILE {t['symbol']}: no live stop/target and no position on our side. Check manually.")
            return
        if protect(b, t, alerts, "Its stop/target orders were no longer live", qty=qty):
            alerts.append(f"{t['symbol']}: had NO live stop/target (orders ended: "
                          f"{', '.join(sorted({str(x.get('status')) for x in exits})) or 'none'}). Put back stop "
                          f"{t['stop']} / target {t['target']} for {qty} sh.")
        o = b.order(t["order_id"])
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
        time_stop(b, t, o, today, notes, alerts)


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
