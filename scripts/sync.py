"""Reconcile journal/trades.jsonl with Alpaca. Run any time; run with --close at 15:50 ET.

Usage:
  python scripts/sync.py            # update fills and exits (target/stop hits) from Alpaca
  python scripts/sync.py --close    # same, plus cancel unfilled entries and apply the 10-day time stop
"""
import argparse
import datetime as dt
import time

import desk as D


def et_date(ts):
    if not ts:
        return D.today_str()
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(D.ET).date().isoformat()


def record_exit(t, price, reason, when):
    t.update({"status": "closed", "exit_price": round(float(price), 4), "exit_reason": reason, "date_close": when})
    t["result_R"] = D.r_multiple(t["direction"], t["entry_fill"], t["stop"], t["exit_price"])
    sign = 1 if t["direction"] == "long" else -1
    t["pnl"] = round(sign * (t["exit_price"] - t["entry_fill"]) * t["shares"], 2)


def trading_days_held(b, since, today):
    try:
        return max(0, len(b.calendar(since, today)) - 1)
    except Exception:
        return 0


def run(close_mode=False):
    b = D.broker()
    today = D.today_str()
    trades = D.read_jsonl(D.TRADES)
    positions = {p["symbol"]: p for p in b.positions()}
    notes, changed = [], False

    for t in trades:
        if t.get("status") not in ("pending", "open", "closing"):
            continue
        try:
            o = b.order(t["order_id"])
        except Exception as e:
            notes.append(f"{t['symbol']}: could not read order {t['order_id']} ({e})")
            continue

        if t["status"] == "closing":
            c = b.order(t["close_order_id"])
            if c.get("status") == "filled":
                record_exit(t, c["filled_avg_price"], "time", et_date(c.get("filled_at")))
                notes.append(f"{t['symbol']}: time-stop exit filled at {t['exit_price']} ({t['result_R']}R)")
                changed = True
            continue

        if o["status"] == "filled" and t.get("entry_fill") is None:
            t.update({"entry_fill": float(o["filled_avg_price"]), "date_fill": et_date(o.get("filled_at")), "status": "open"})
            notes.append(f"{t['symbol']}: entry filled at {t['entry_fill']}")
            changed = True

        if t.get("entry_fill") is not None:
            leg = next((l for l in (o.get("legs") or []) if l.get("status") == "filled"), None)
            if leg:
                reason = "target" if leg.get("type") == "limit" else "stop"
                record_exit(t, leg["filled_avg_price"], reason, et_date(leg.get("filled_at")))
                notes.append(f"{t['symbol']}: {reason} hit at {t['exit_price']} ({t['result_R']}R, ${t['pnl']})")
                changed = True
                continue
            if t["symbol"] not in positions:
                notes.append(f"RECONCILE {t['symbol']}: journal says open but Alpaca shows no position and no exit leg. Check manually.")
        elif o["status"] in ("canceled", "expired", "rejected"):
            t.update({"status": "unfilled", "date_close": today, "exit_reason": o["status"]})
            notes.append(f"{t['symbol']}: entry {o['status']}, marked unfilled")
            changed = True
            continue

        if not close_mode:
            continue

        if t["status"] == "pending" and o["status"] == "partially_filled":
            notes.append(f"{t['symbol']}: entry PARTIALLY filled; left alone. Tell Chris.")
        elif t["status"] == "pending":
            b.cancel_order(t["order_id"])
            t.update({"status": "unfilled", "date_close": today, "exit_reason": "unfilled"})
            notes.append(f"{t['symbol']}: entry never filled, cancelled")
            changed = True
        elif t["status"] == "open":
            held = trading_days_held(b, t["date_fill"], today)
            t["days_held"] = held
            if held >= D.TIME_STOP_DAYS:
                for l in o.get("legs") or []:
                    if l.get("status") in ("new", "accepted", "held", "pending_new", "partially_filled"):
                        try:
                            b.cancel_order(l["id"])
                        except Exception:
                            pass
                time.sleep(1)
                c = b.close_position(t["symbol"])
                t.update({"status": "closing", "close_order_id": c["id"]})
                time.sleep(2)
                c2 = b.order(c["id"])
                if c2.get("status") == "filled":
                    record_exit(t, c2["filled_avg_price"], "time", today)
                    notes.append(f"{t['symbol']}: 10-day time stop, closed at {t['exit_price']} ({t['result_R']}R)")
                else:
                    notes.append(f"{t['symbol']}: 10-day time stop, close order sent ({c['id']}); fill recorded on next sync")
                changed = True

    if changed:
        D.write_jsonl(D.TRADES, trades)

    print(f"SYNC {today}{' (CLOSE)' if close_mode else ''}")
    print("\n".join(notes) if notes else "No changes.")
    print("OPEN POSITIONS:")
    any_open = False
    for t in trades:
        if t.get("status") == "open":
            any_open = True
            p = positions.get(t["symbol"])
            px = float(p["current_price"]) if p else None
            r = D.r_multiple(t["direction"], t["entry_fill"], t["stop"], px) if px else None
            print(f"  {t['symbol']} {t['direction']} tier {t['tier']} | entry {t['entry_fill']} now {px} | "
                  f"{r}R unrealized | days held {t.get('days_held', '?')}")
    if not any_open:
        print("  none")
    closed_today = [t for t in trades if t.get("status") == "closed" and t.get("date_close") == today]
    if closed_today:
        print("CLOSED TODAY (write a one-sentence lesson for each):")
        for t in closed_today:
            print(f"  {t['trade_id']}: {t['exit_reason']} {t['result_R']}R ${t['pnl']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--close", action="store_true")
    run(ap.parse_args().close)
