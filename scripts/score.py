"""Score the universe and (in decision mode) build today's order plan under rules v1.0.

Usage:
  python scripts/score.py --mode briefing            # 08:45 ET: score on yesterday's closes, no orders, no logging
  python scripts/score.py --mode decision            # 10:15 ET: live prices, risk limits, writes the plan + logs candidates
  python scripts/score.py --mode controls            # backfill today's random control entries only (no plan, no orders)
Options:
  --regime FILE      regime scores Socrates wrote (default: reports/regime_<today>.json if present)
  --exclude A,B      stocks to skip today for earnings Socrates found himself. Prefer the regime file's
                     "exclude" block, which records the date and source; ETFs can never be excluded.
  --force            rerun DECISION after it already ran today (logged and flagged in the weekly review)
  --json             print raw JSON instead of the summary
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import platform

import broker as BR
import control as C
import desk as D

FINNHUB_URL = "https://finnhub.io/api/v1/calendar/earnings"
EARNINGS_DAYS = 10


def load_regime(path):
    """Returns (regime_dict, relative_path_or_None, sha_or_None, warnings). Refuses to score on an invalid file."""
    if not (path and os.path.exists(path)):
        return {}, None, None, []
    rp = os.path.realpath(path)
    if os.path.dirname(rp) != os.path.realpath(D.REPORTS) or not os.path.basename(rp).startswith("regime_") \
            or not rp.endswith(".json"):
        raise D.DeskError("REGIME FILE INVALID: it must be reports/regime_<YYYY-MM-DD>.json inside the desk folder.")
    if os.path.getsize(rp) > 100_000:
        raise D.DeskError("REGIME FILE INVALID: the file is over 100 KB. Keep it to the format in the rules.")
    try:
        with open(rp) as f:
            reg = json.load(f)
    except (OSError, ValueError, RecursionError) as e:
        raise D.DeskError(f"REGIME FILE INVALID: {os.path.basename(rp)} is not valid JSON ({type(e).__name__}). "
                          "Fix the file and rerun.")
    path = os.path.relpath(rp, D.ROOT)
    errors, warns = D.validate_regime(reg)
    if errors:
        raise D.DeskError("REGIME FILE INVALID: " + path + "\n  - " + "\n  - ".join(errors) +
                          "\nFix the file and rerun score.py (this is the one error you fix yourself).")
    return reg, path, D.sha256_file(rp), [f"regime file: {w}" for w in warns]


def regime_points(reg, symbol, direction):
    """Symbol entry beats theme entry; anything missing is neutral (1). The file was validated on load."""
    for scope, key in (("symbols", symbol), ("themes", D.THEME_OF.get(symbol))):
        node = (reg.get(scope) or {}).get(key) or {}
        if direction in node:
            return int(node[direction]), node.get("why", "")
    return 1, "neutral (no regime entry)"


def _finnhub_get(session, sym, start, end, key):
    # token in a header, not the URL, so it can never appear in a logged or printed URL
    r = session.get(FINNHUB_URL, params={"symbol": sym, "from": start, "to": end},
                    headers={"X-Finnhub-Token": key}, timeout=20, allow_redirects=False)
    r.raise_for_status()
    rows = r.json().get("earningsCalendar")
    if not isinstance(rows, list):
        raise ValueError("unexpected response shape")
    return rows


def earnings_blackout(b, today):
    """Which stocks report within the next 10 trading days (today included), checked one stock at a time.
    Returns (blackout, verified, note). A stock is 'verified' only if its own lookup answered cleanly; anything
    else stays unverified and must be checked by hand before it can be placed."""
    key = os.environ.get("FINNHUB_API_KEY")
    if not key:
        return set(), set(), "no FINNHUB_API_KEY in the Keychain"
    try:
        end = D.trading_days_ahead(b, today, EARNINGS_DAYS)
    except Exception as e:
        return set(), set(), f"market calendar unavailable ({type(e).__name__})"
    session = BR.retrying_session()
    blackout, verified, failed = set(), set(), []
    for sym in D.STOCKS:
        try:
            rows = _finnhub_get(session, sym, today, end, key)
        except Exception as e:      # never echo the exception text: it can contain the URL with the token
            failed.append(f"{sym} ({type(e).__name__})")
            continue
        verified.add(sym)
        if any(x.get("symbol") == sym and today <= str(x.get("date") or "") <= end for x in rows):
            blackout.add(sym)
    note = f"checked {len(verified)}/{len(D.STOCKS)} stocks through {end}"
    if failed:
        note += "; lookup FAILED for " + ", ".join(failed)
    return blackout, verified, note


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


def ensure_start_file(spy_close, equity, today):
    txt = D.START_FILE.read_text().strip() if D.START_FILE.exists() else "PENDING"
    if "PENDING" in txt or not txt:
        D.START_FILE.write_text(json.dumps({"date": today, "spy_close": round(spy_close, 2), "equity": round(equity, 2)}) + "\n")


def market_guard(b, mode, today):
    """DECISION and controls only run while the market is open on a real session. Never guess."""
    try:
        trading_day, is_open, close = D.market_status(b)
    except Exception as e:
        raise D.DeskError(f"MARKET CLOCK UNAVAILABLE ({type(e).__name__}: {e}). Nothing was scored or logged. "
                          "Tell Chris; do not retry in a loop.")
    if mode in ("decision", "controls"):
        if not trading_day:
            raise D.DeskError(f"MARKET CLOSED TODAY ({today} is a weekend or market holiday). No decision run, "
                              "nothing logged. Report 'market closed' and stop.", )
        if not is_open:
            raise D.DeskError(f"MARKET NOT OPEN RIGHT NOW (today's session closes {close} ET). DECISION only runs "
                              "during market hours. Nothing logged. Report this and stop.")
    return trading_day, close


def run(mode, regime_path=None, exclude=(), force=False):
    with D.journal_lock():
        return _run(mode, regime_path, exclude, force)


def _run(mode, regime_path, exclude, force):
    b = D.broker()
    today = D.today_str()
    rv = D.rules_version()
    live = mode in ("decision", "controls")
    warnings = []

    trading_day, _close = market_guard(b, mode, today)
    if not trading_day:
        warnings.append(f"{today} is not a trading day; this briefing scores the last completed session.")

    plan_file = D.REPORTS / f"plan_{today}.json"
    rerun_n = 0
    if mode == "decision" and plan_file.exists():
        if not force:
            raise D.DeskError(f"DECISION ALREADY RAN TODAY ({plan_file.name} exists). Do not rerun it: place the "
                              "orders in that plan. If the first run crashed or Chris asked for a rerun, use --force "
                              "(it is logged and shows up in the weekly review).")
        rerun_n = 1 + len(list(D.REPORTS.glob(f"plan_{today}_superseded*.json")))

    reg, reg_file, reg_sha, reg_warn = load_regime(regime_path or str(D.REPORTS / f"regime_{today}.json"))
    warnings += reg_warn
    if not reg_file:
        warnings.append("No regime file: regime = 1 (neutral) for everything. Write reports/regime_<date>.json first.")

    # exclusions: the regime file's "exclude" block (with reasons) plus --exclude (stocks only)
    exclusions = {s: D.plain_text(why, 200) for s, why in (reg.get("exclude") or {}).items()}
    for s in exclude:
        if s not in D.STOCKS:
            raise D.DeskError(f"REFUSED: --exclude {s}: only single stocks can be excluded (for earnings). "
                              "ETFs have no earnings; excluding one would be a veto (hard rule 3).")
        exclusions.setdefault(s, "--exclude (no reason recorded)")

    acct = b.account()
    equity = float(acct["equity"])
    last_equity = float(acct.get("last_equity") or equity)

    start = (D.now_et() - dt.timedelta(days=150)).date().isoformat() + "T00:00:00Z"
    raw = b.daily_bars(D.UNIVERSE, start, D.bars_end(), adjustment="all")
    feed = getattr(b, "bars_feed", "test")
    try:
        last_session = D.previous_trading_day(b, today)
    except Exception as e:
        raise D.DeskError(f"MARKET CALENDAR UNAVAILABLE ({type(e).__name__}: {e}). Cannot check the data is current. "
                          "Nothing logged. Tell Chris.")
    today_d = dt.date.fromisoformat(today)
    frames = {}
    for s in D.UNIVERSE:
        df = D.completed(D.bars_to_df(raw.get(s, [])), today_d)
        if len(df) < D.MIN_BARS:
            warnings.append(f"{s}: only {len(df)} daily bars, skipped")
        elif str(df["date"].iloc[-1]) != last_session:
            warnings.append(f"{s}: newest bar is {df['date'].iloc[-1]}, expected {last_session}. Stale data, skipped")
        else:
            frames[s] = df
    if "SPY" not in frames:
        raise D.DeskError("SPY bars unavailable or stale; cannot score. Nothing logged. Check Alpaca data access.")
    if feed == "iex":
        warnings.append("Bars came from the IEX feed (SIP refused): volume is a small slice of the market.")
    spy_ret20 = D.ret(frames["SPY"], 20)

    if mode == "decision":
        ensure_start_file(float(frames["SPY"]["close"].iloc[-1]), equity, today)

    blackout, verified, enote = earnings_blackout(b, today)
    if len(verified) < len(D.STOCKS):
        warnings.append(f"Earnings calendar incomplete ({enote}). Unverified stocks are marked EARNINGS UNVERIFIED.")

    prices = b.latest_prices(list(frames)) if live else {}

    cands = []
    both = {}
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
            if s in D.STOCKS:
                c["earnings_unverified"] = s not in verified
            both.setdefault(s, {})[d] = c
            if best is None or (c["score"], c["rr"]) > (best["score"], best["rr"]):
                best = c
        cands.append(best)
    cands.sort(key=lambda c: (c["score"], c["rr"]), reverse=True)

    result = {"date": today, "mode": mode, "rules_version": rv, "equity": equity,
              "spy_ret20": round(spy_ret20, 4), "regime_file": reg_file, "regime_sha": reg_sha,
              "bars_feed": feed, "earnings_check": enote, "exclusions": exclusions, "warnings": warnings}

    if mode == "controls":
        crows, cnote = C.draw(both, blackout, b, today, verified, backfill=True)
        result.update({"controls": crows, "controls_drawn": len(crows), "controls_note": cnote})
        _save(result, f"controls_{today}.json")
        return result

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
    for o in orders:
        if o["symbol"] not in tracked and o["symbol"] not in {p["symbol"] for p in positions}:
            open_risk += equity * D.RISK_PCT["A"]
            warnings.append(f"{o['symbol']}: open order not in trades.jsonl. Counted as 0.5% risk. Tell Chris.")
    theme_count = {}
    for s in busy:
        theme_count[D.THEME_OF.get(s)] = theme_count.get(D.THEME_OF.get(s), 0) + 1
    day_pnl = (equity - last_equity) / last_equity if last_equity else 0.0
    halted = day_pnl <= D.DAILY_HALT_PCT
    state = {"n_open": len(busy), "open_risk": open_risk}

    def check(c, tier):
        if c["symbol"] in exclusions:
            return f"excluded today: {exclusions[c['symbol']]}"
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
    try:
        idle = D.trading_days_between(b, ref or today, today)
    except Exception as e:
        idle = None
        warnings.append(f"QUOTA CHECK SKIPPED: market calendar unavailable ({type(e).__name__}). Tell Chris.")
    result["trading_days_since_last_AB"] = idle
    if not plan and idle is not None and idle >= D.QUOTA_DAYS:
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
        c["equity_at_plan"] = equity

    run_name = "decision" if not rerun_n else f"decision-rerun{rerun_n}"
    try:   # the control group must never be able to block trading
        crows, cnote = C.draw(both, blackout, b, today, verified)
    except Exception as e:
        crows, cnote = [], f"control draw failed ({type(e).__name__}: {e}); backfill later with --mode controls"
    result.update({"run": run_name, "day_pnl_pct": round(day_pnl, 4), "halted": halted,
                   "open_positions": len(busy) - len(plan), "open_risk_dollars": round(open_risk, 2), "plan": plan,
                   "blocked": [c for c in cands if c["action"] == "blocked"][:5],
                   "shadow_top": [c for c in cands if c["action"] == "shadow"][:3],
                   "controls_drawn": len(crows), "controls_note": cnote})

    # The plan is written before the candidate log, so a crash can never leave logged candidates without a plan
    # (which would force a --force rerun and duplicate rows).
    if rerun_n:
        (D.REPORTS / f"plan_{today}.json").replace(D.REPORTS / f"plan_{today}_superseded{rerun_n}.json")
    plan_sha = _save(result, f"plan_{today}.json")
    D.append_jsonl(D.CANDIDATES, [{
        "date": today, "run": run_name, "rules_version": rv, "symbol": c["symbol"], "direction": c["direction"],
        "score": c["score"], "breakdown": c["breakdown"], "entry": c["entry"], "stop": c["stop"],
        "target": c["target"], "rr": c["rr"], "tier": c["tier"], "quota": c.get("quota", False),
        "action": c["action"], "block_reason": c.get("block_reason"), "p_target": None,
        "regime_sha": reg_sha, "plan_sha": plan_sha, "bars_feed": feed, "excluded": c["symbol"] in exclusions,
        "py": platform.python_version()} for c in cands])
    return result


def _save(result, name):
    """Write a report atomically; returns the sha256 of what was written (the plan's fingerprint)."""
    text = json.dumps(result, indent=2)
    D.atomic_write(D.REPORTS / name, text)
    return hashlib.sha256(text.encode()).hexdigest()


def line(c):
    b = c["breakdown"]
    return (f"{c['symbol']} {c['direction'].upper()} {c['score']}/10 (trend {b['trend']}, rs {b['rs']}, regime {b['regime']}, "
            f"rr {b['rr']}, mom {b['momentum']}, vol {b['volume']}) entry {c['entry']} stop {c['stop']} "
            f"target {c['target']} R:R {c['rr']}")


def summary(r):
    out = [f"SCORE {r['mode'].upper()} {r['date']} | rules {r['rules_version']} | equity ${r['equity']:,.2f}"]
    out += [f"WARNING: {w}" for w in r["warnings"]]
    if r.get("exclusions"):
        out.append("EXCLUDED TODAY: " + "; ".join(f"{s} ({why})" for s, why in r["exclusions"].items()))
    if r["mode"] == "controls":
        out.append(f"CONTROL BASELINE (backfill): drew {r['controls_drawn']} random control entries. {r['controls_note']}")
        out += [f"  {x['symbol']} {x['direction'].upper()} entry {x['entry']} stop {x['stop']} target {x['target']} R:R {x['rr']}"
                for x in r["controls"]]
        return "\n".join(out)
    if r["mode"] == "briefing":
        out.append("TOP 5 CANDIDATES:")
        out += ["  " + line(c) for c in r["top"]]
        return "\n".join(out)
    if r.get("run", "decision") != "decision":
        out.append(f"NOTE: this is a forced rerun ({r['run']}); the earlier plan was kept as plan_{r['date']}_superseded*.json.")
    idle = r["trading_days_since_last_AB"]
    out.append(f"Day P/L {r['day_pnl_pct']:.2%} | halted: {r['halted']} | open positions before today: {r['open_positions']} "
               f"| open risk ${r['open_risk_dollars']:,.2f} | trading days since last A/B trade: {idle if idle is not None else 'UNKNOWN'}")
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
    out.append(f"CONTROL BASELINE: {r.get('controls_drawn', 0)} random control entries logged automatically. {r.get('controls_note', '')}")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["briefing", "decision", "controls"], required=True)
    ap.add_argument("--regime")
    ap.add_argument("--exclude", default="")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    res = run(a.mode, a.regime, tuple(x.strip().upper() for x in a.exclude.split(",") if x.strip()), a.force)
    text = summary(res)
    # score_<mode>_<date>.md, so the script never overwrites the briefing/decision report Socrates writes himself
    (D.REPORTS / f"score_{a.mode}_{res['date']}.md").write_text(f"# Score {a.mode} {res['date']}\n\n```\n{text}\n```\n")
    print(json.dumps(res, indent=2) if a.json else text)


if __name__ == "__main__":
    main()
