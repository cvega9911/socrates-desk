"""Score the universe and (in decision mode) build today's order plan under rules v1.0.

Usage:
  python scripts/score.py --mode briefing            # 08:45 ET: score on yesterday's closes, no orders, no logging
  python scripts/score.py --mode decision            # 10:15 ET: live prices, risk limits, writes the plan + logs candidates
Options:
  --regime FILE      regime scores Socrates wrote (default: reports/regime_<today>.json if present)
  --exclude A,B      symbols to skip today (e.g. earnings Socrates found himself)
  --json             print raw JSON instead of the summary
"""
import argparse
import datetime as dt
import json
import os
import sys

import requests

import desk as D


def load_regime(path):
    if path and os.path.exists(path):
        with open(path) as f:
            return json.load(f), path
    return {}, None


def regime_points(reg, symbol, direction):
    """Symbol entry beats theme entry; anything missing is neutral (1)."""
    for scope, key in (("symbols", symbol), ("themes", D.THEME_OF.get(symbol))):
        node = (reg.get(scope) or {}).get(key) or {}
        if direction in node:
            pts = int(node[direction])
            return max(0, min(2, pts)), node.get("why", "")
    return 1, "neutral (no regime entry)"


def earnings_blackout(today):
    """Stocks reporting in the next ~10 trading days. Returns (set, verified_bool)."""
    key = os.environ.get("FINNHUB_API_KEY")
    if not key:
        return set(), False
    end = (dt.date.fromisoformat(today) + dt.timedelta(days=15)).isoformat()
    try:
        r = requests.get("https://finnhub.io/api/v1/calendar/earnings",
                         params={"from": today, "to": end, "token": key}, timeout=20)
        r.raise_for_status()
        rows = r.json().get("earningsCalendar") or []
        return {x["symbol"] for x in rows if x.get("symbol") in D.STOCKS}, True
    except Exception:
        return set(), False


def entry_price(px, close, direction, live):
    if not live:
        return round(close, 2)
    bid, ask, last = px.get("bid", 0), px.get("ask", 0), px.get("last", 0)
    sane = bid > 0 and ask > 0 and ask >= bid and (ask - bid) / ((ask + bid) / 2) < 0.005
    if direction == "long":
        base = ask if sane else (last or close)
        return round(base * (1 + D.ENTRY_SLIP), 2)
    base = bid if sane else (last or close)
    return round(base * (1 - D.ENTRY_SLIP), 2)


def trading_days_since(b, ref_date, today):
    if ref_date >= today:
        return 0
    try:
        return max(0, len(b.calendar(ref_date, today)) - 1)
    except Exception:
        return 0


def ensure_start_file(spy_close, equity, today):
    txt = D.START_FILE.read_text().strip() if D.START_FILE.exists() else "PENDING"
    if "PENDING" in txt or not txt:
        D.START_FILE.write_text(json.dumps({"date": today, "spy_close": round(spy_close, 2), "equity": round(equity, 2)}) + "\n")


def run(mode, regime_path=None, exclude=()):
    b = D.broker()
    today = D.today_str()
    live = mode == "decision"
    warnings = []

    acct = b.account()
    equity = float(acct["equity"])
    last_equity = float(acct.get("last_equity") or equity)

    start = (D.now_et() - dt.timedelta(days=150)).date().isoformat() + "T00:00:00Z"
    end = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=16)).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw = b.daily_bars(D.UNIVERSE, start, end)
    frames = {}
    for s in D.UNIVERSE:
        df = D.completed(D.bars_to_df(raw.get(s, [])), dt.date.fromisoformat(today))
        if len(df) >= 61:
            frames[s] = df
        else:
            warnings.append(f"{s}: only {len(df)} daily bars, skipped")
    if "SPY" not in frames:
        raise SystemExit("SPY bars unavailable; cannot score. Check Alpaca data access.")
    spy_ret20 = D.ret(frames["SPY"], 20)

    if live:
        ensure_start_file(float(frames["SPY"]["close"].iloc[-1]), equity, today)

    reg, reg_file = load_regime(regime_path or str(D.REPORTS / f"regime_{today}.json"))
    if not reg_file:
        warnings.append("No regime file: regime = 1 (neutral) for everything. Write reports/regime_<date>.json first.")

    blackout, verified = earnings_blackout(today)
    if not verified:
        warnings.append("Earnings calendar UNAVAILABLE (no FINNHUB_API_KEY): stock candidates are marked earnings_unverified.")

    prices = b.latest_prices(list(frames)) if live else {}

    cands = []
    for s, df in frames.items():
        f = D.factors(df, spy_ret20)
        best = None
        for d in ("long", "short"):
            e = entry_price(prices.get(s, {}), f["close"], d, live)
            stop, target, rr = D.levels(f, e, d)
            pts, why = regime_points(reg, s, d)
            total, br = D.score(f, d, pts, rr, is_spy=(s == "SPY"))
            c = {"symbol": s, "direction": d, "score": total, "breakdown": br, "entry": e, "stop": stop,
                 "target": target, "rr": rr, "tier": D.tier_for(total, rr), "theme": D.THEME_OF.get(s),
                 "regime_why": why, "atr": round(f["atr"], 2), "close": round(f["close"], 2)}
            if best is None or (c["score"], c["rr"]) > (best["score"], best["rr"]):
                best = c
        if s in D.STOCKS:
            best["earnings_unverified"] = not verified
        cands.append(best)
    cands.sort(key=lambda c: (c["score"], c["rr"]), reverse=True)

    result = {"date": today, "mode": mode, "rules_version": D.rules_version(), "equity": equity,
              "spy_ret20": round(spy_ret20, 4), "regime_file": reg_file, "warnings": warnings}

    if not live:
        result["top"] = cands[:5]
        result["all"] = cands
        _save(result, f"score_briefing_{today}.json")
        return result

    # ---------------- decision mode: risk engine ----------------
    positions = b.positions()
    orders = b.open_orders()
    jopen = D.open_trades()
    tracked = {t["symbol"] for t in jopen}
    busy = {p["symbol"] for p in positions} | {o["symbol"] for o in orders} | tracked
    open_risk = sum(float(t.get("risk_dollars") or 0) for t in jopen)
    for p in positions:
        if p["symbol"] not in tracked:
            open_risk += equity * D.RISK_PCT["A"]
            warnings.append(f"{p['symbol']}: position not in trades.jsonl (manual?). Counted as 0.5% risk.")
    theme_count = {}
    for s in busy:
        theme_count[D.THEME_OF.get(s)] = theme_count.get(D.THEME_OF.get(s), 0) + 1
    day_pnl = (equity - last_equity) / last_equity if last_equity else 0.0
    halted = day_pnl <= D.DAILY_HALT_PCT
    state = {"n_open": len(busy), "open_risk": open_risk}

    def check(c, tier):
        if c["symbol"] in exclude:
            return "excluded today (--exclude)"
        if c["symbol"] in blackout:
            return "universe rule: earnings within 10 trading days"
        if halted:
            return "risk rule 1: daily halt (P/L <= -1.5%)"
        if state["n_open"] >= D.MAX_POSITIONS:
            return "risk rule 2: max 5 open positions"
        shares = D.size(equity, D.RISK_PCT[tier], c["entry"], c["stop"])
        risk = round(shares * abs(c["entry"] - c["stop"]), 2)
        if state["open_risk"] + risk > equity * D.MAX_OPEN_RISK_PCT:
            return "risk rule 3: total open risk would exceed 2.5%"
        if theme_count.get(c["theme"], 0) >= D.MAX_PER_THEME:
            return f"risk rule 4: already 2 positions in theme '{c['theme']}'"
        if c["symbol"] in busy:
            return "risk rule 5: already have a position or order in this symbol"
        if shares < 1:
            return "risk rule 6: size rounds to 0 shares"
        if c["direction"] == "short":
            try:
                a = b.asset(c["symbol"])
                if not (a.get("shortable") and a.get("easy_to_borrow")):
                    return "order rule: not shortable / easy-to-borrow"
            except Exception:
                return "order rule: could not confirm shortable"
        c["shares"], c["risk_dollars"] = shares, risk
        return None

    def commit(c):
        state["n_open"] += 1
        state["open_risk"] += c["risk_dollars"]
        theme_count[c["theme"]] = theme_count.get(c["theme"], 0) + 1
        busy.add(c["symbol"])

    plan = []
    for c in cands:
        c["quota"] = False
        if c["tier"] in ("A", "B"):
            why = check(c, c["tier"])
            if why:
                c["action"], c["block_reason"] = "blocked", why
            else:
                c["action"], c["block_reason"] = "planned", None
                commit(c)
                plan.append(c)
        else:
            c["action"] = "shadow"
            c["block_reason"] = f"R:R {c['rr']} below {D.MIN_RR}" if c["rr"] < D.MIN_RR else None

    # quota rule
    ab = [t for t in D.read_jsonl(D.TRADES) if t.get("tier") in ("A", "B")]
    ref = max([t["date_open"] for t in ab], default=None)
    if ref is None and D.START_FILE.exists():
        try:
            ref = json.loads(D.START_FILE.read_text())["date"]
        except Exception:
            ref = today
    idle = trading_days_since(b, ref or today, today)
    result["trading_days_since_last_AB"] = idle
    if not plan and idle >= D.QUOTA_DAYS:
        for c in cands:
            if c["score"] >= D.QUOTA_MIN_SCORE and c["rr"] >= D.MIN_RR and c["action"] in ("shadow", "blocked"):
                if c["action"] == "blocked" and not c["block_reason"].startswith("risk rule 3"):
                    continue
                if check(c, "B") is None:
                    c.update({"tier": "B", "quota": True, "action": "planned", "block_reason": None})
                    commit(c)
                    plan.append(c)
                    break

    for c in plan:
        c["risk_pct"] = D.RISK_PCT[c["tier"]]

    rv = D.rules_version()
    D.append_jsonl(D.CANDIDATES, [{
        "date": today, "run": "decision", "rules_version": rv, "symbol": c["symbol"], "direction": c["direction"],
        "score": c["score"], "breakdown": c["breakdown"], "entry": c["entry"], "stop": c["stop"],
        "target": c["target"], "rr": c["rr"], "tier": c["tier"], "quota": c.get("quota", False),
        "action": c["action"], "block_reason": c.get("block_reason"), "p_target": None} for c in cands])

    result.update({"day_pnl_pct": round(day_pnl, 4), "halted": halted, "open_positions": len(busy) - len(plan),
                   "open_risk_dollars": round(open_risk, 2), "plan": plan,
                   "blocked": [c for c in cands if c["action"] == "blocked"][:5],
                   "shadow_top": [c for c in cands if c["action"] == "shadow"][:3]})
    _save(result, f"plan_{today}.json")
    return result


def _save(result, name):
    D.REPORTS.mkdir(parents=True, exist_ok=True)
    (D.REPORTS / name).write_text(json.dumps(result, indent=2))


def line(c):
    b = c["breakdown"]
    return (f"{c['symbol']} {c['direction'].upper()} {c['score']}/10 (trend {b['trend']}, rs {b['rs']}, regime {b['regime']}, "
            f"rr {b['rr']}, mom {b['momentum']}, vol {b['volume']}) entry {c['entry']} stop {c['stop']} "
            f"target {c['target']} R:R {c['rr']}")


def summary(r):
    out = [f"SCORE {r['mode'].upper()} {r['date']} | rules {r['rules_version']} | equity ${r['equity']:,.2f}"]
    out += [f"WARNING: {w}" for w in r["warnings"]]
    if r["mode"] == "briefing":
        out.append("TOP 5 CANDIDATES:")
        out += ["  " + line(c) for c in r["top"]]
        return "\n".join(out)
    out.append(f"Day P/L {r['day_pnl_pct']:.2%} | halted: {r['halted']} | open positions before today: {r['open_positions']} "
               f"| open risk ${r['open_risk_dollars']:,.2f} | trading days since last A/B trade: {r['trading_days_since_last_AB']}")
    if r["plan"]:
        out.append("PLANNED ORDERS (place each with place.py):")
        for c in r["plan"]:
            tag = " QUOTA" if c.get("quota") else ""
            flag = " [EARNINGS UNVERIFIED: check before placing]" if c.get("earnings_unverified") else ""
            out.append(f"  Tier {c['tier']}{tag}: {line(c)} | {c['shares']} sh | ${c['risk_dollars']} at risk{flag}")
    else:
        top = r["blocked"][0] if r["blocked"] else (r["shadow_top"][0] if r["shadow_top"] else None)
        out.append("NO TRADE." + (f" Top candidate: {line(top)}. Failed: {top.get('block_reason') or 'score below 5'}" if top else ""))
        out.append("Socrates: add 'Would trade if: <specific price, level or event>' for that candidate.")
    if r["blocked"]:
        out.append("BLOCKED:")
        out += [f"  {line(c)} -> {c['block_reason']}" for c in r["blocked"]]
    out.append("SHADOW BOOK:")
    out += ["  " + line(c) for c in r["shadow_top"]]
    return "\n".join(out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["briefing", "decision"], required=True)
    ap.add_argument("--regime")
    ap.add_argument("--exclude", default="")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    res = run(a.mode, a.regime, tuple(x.strip().upper() for x in a.exclude.split(",") if x.strip()))
    text = summary(res)
    (D.REPORTS / f"{a.mode}_{res['date']}.md").write_text(f"# {a.mode.title()} {res['date']}\n\n```\n{text}\n```\n")
    print(json.dumps(res, indent=2) if a.json else text)
