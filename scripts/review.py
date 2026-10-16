"""Weekly review for rules v1.0. Appends a markdown report to journal/weekly.md.

Usage:
  python scripts/review.py              # full review (needs Alpaca for benchmark + shadow book)
  python scripts/review.py --offline    # journal-only stats, no Alpaca calls
"""
import argparse
import datetime as dt
import json

import pandas as pd

import control as C
import desk as D


def pct(x):
    return "n/a" if x is None else f"{x:.1%}"


def stats(rows):
    rs = [t["result_R"] for t in rows if t.get("result_R") is not None]
    if not rs:
        return {"n": 0}
    return {"n": len(rs), "win_rate": sum(r > 0 for r in rs) / len(rs), "avg_R": sum(rs) / len(rs)}


def stats_r(rs):
    if not rs:
        return None
    return {"n": len(rs), "win_rate": sum(r > 0 for r in rs) / len(rs), "avg_R": sum(rs) / len(rs)}


def max_drawdown(closed, start_equity):
    eq, peak, mdd = start_equity, start_equity, 0.0
    for t in sorted(closed, key=lambda t: t["date_close"]):
        eq += t.get("pnl") or 0
        peak = max(peak, eq)
        mdd = min(mdd, (eq - peak) / peak)
    return mdd


def run(offline=False):
    today = D.today_str()
    today_d = dt.date.fromisoformat(today)
    rv = D.rules_version()
    trades = [t for t in D.read_jsonl(D.TRADES) if t.get("rules_version", rv) == rv]
    cands = [c for c in D.read_jsonl(D.CANDIDATES) if c.get("run") == "decision" and c.get("rules_version", rv) == rv]
    closed = [t for t in trades if t.get("status") == "closed"]
    week_ago = (today_d - dt.timedelta(days=7)).isoformat()

    try:
        start = json.loads(D.START_FILE.read_text())
    except Exception:
        start = None
    start_equity = (start or {}).get("equity", 100000.0)

    L = [f"\n## Weekly review {today} (rules {rv})\n"]
    if len(closed) < 20:
        L.append(f"**Sample too small for conclusions** ({len(closed)} closed trades; need 20 to interpret, 40 to change rules).\n")

    # 1. Activity
    def by_tier(rows):
        return (f"A {sum(t['tier']=='A' for t in rows)}, B {sum(t['tier']=='B' and not t.get('quota') for t in rows)}, "
                f"quota {sum(bool(t.get('quota')) for t in rows)}")
    opened_wk = [t for t in trades if t["date_open"] >= week_ago]
    closed_wk = [t for t in closed if (t.get("date_close") or "") >= week_ago]
    L.append("### 1. Activity")
    L.append(f"- This week: {len(opened_wk)} opened ({by_tier(opened_wk)}), {len(closed_wk)} closed")
    L.append(f"- To date: {len(trades)} placed ({by_tier(trades)}), {len(closed)} closed, "
             f"{sum(t['status']=='unfilled' for t in trades)} unfilled, {sum(t['status'] in ('open','pending','closing') for t in trades)} still open/pending")

    # 2. Performance
    s = stats(closed)
    L.append("\n### 2. Performance to date")
    if s["n"]:
        L.append(f"- Win rate {pct(s['win_rate'])} | expectancy (avg R per trade) {s['avg_R']:+.2f}R | "
                 f"total P/L ${sum(t.get('pnl') or 0 for t in closed):,.2f} | max drawdown {pct(max_drawdown(closed, start_equity))}")
        for tier in ("A", "B"):
            st = stats([t for t in closed if t["tier"] == tier])
            if st["n"]:
                L.append(f"- Tier {tier}: {st['n']} trades, win {pct(st['win_rate'])}, {st['avg_R']:+.2f}R avg")
    else:
        L.append("- No closed trades yet.")

    # 3. Benchmark
    L.append("\n### 3. Benchmark vs SPY")
    bars = {}
    if offline or not start:
        L.append("- Skipped (offline or start_date.txt not initialised yet).")
    else:
        b = D.broker()
        equity = float(b.account()["equity"])
        syms = sorted({c["symbol"] for c in cands} | {"SPY"})
        s0 = (dt.date.fromisoformat(min([c["date"] for c in cands] + [start["date"]])) - dt.timedelta(days=5)).isoformat()
        end = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=16)).strftime("%Y-%m-%dT%H:%M:%SZ")
        raw = b.daily_bars(syms, s0 + "T00:00:00Z", end)
        full = {k: D.bars_to_df(v) for k, v in raw.items()}
        spy_now = float(full["SPY"]["close"].iloc[-1])
        bars = {k: D.completed(v, today_d) for k, v in full.items()}
        acct_chg = equity / start_equity - 1
        spy_chg = spy_now / start["spy_close"] - 1
        L.append(f"- Since {start['date']}: account {pct(acct_chg)} vs SPY {pct(spy_chg)} "
                 f"({'ahead' if acct_chg > spy_chg else 'behind'} by {abs(acct_chg - spy_chg):.1%})")
        L.append("- Note: the account is mostly cash and mixes longs with shorts, so this line mostly reflects market exposure, "
                 "not skill. Judge the edge on R and on the random baseline (5b), not this line.")

    # 4. Score buckets
    L.append("\n### 4. Score buckets (closed trades)")
    for label, lo, hi in (("4", 4, 4), ("5", 5, 5), ("6", 6, 6), ("7", 7, 7), ("8+", 8, 10)):
        st = stats([t for t in closed if lo <= t["score"] <= hi])
        L.append(f"- Score {label}: " + (f"{st['n']} trades, {st['avg_R']:+.2f}R avg, win {pct(st['win_rate'])}" if st["n"] else "none"))

    # 5. Shadow book (simulated forward from the NEXT daily bar, same simulator as the random controls)
    L.append("\n### 5. Shadow book (hypothetical, 10-day horizon, same-day ties counted as stops)")
    groups = {"planned": [], "blocked": [], "shadow": []}
    if bars:
        seen = set()
        for c in cands:
            key = (c["date"], c["symbol"])
            if key in seen or c["symbol"] not in bars or c["action"] not in groups:
                continue
            seen.add(key)
            out = D.simulate_path(bars[c["symbol"]], c["direction"], c["entry"], c["stop"], c["target"],
                                  dt.date.fromisoformat(c["date"]))
            if out is not None:
                groups[c["action"]].append(out[0])
        for g, rs in groups.items():
            L.append(f"- {g}: " + (f"{len(rs)} resolved, {sum(rs)/len(rs):+.2f}R avg, win {sum(r > 0 for r in rs)/len(rs):.0%}" if rs else "none resolved yet"))
        p, sh = groups["planned"], groups["shadow"]
        if len(p) >= 10 and len(sh) >= 10:
            gap = sum(p) / len(p) - sum(sh) / len(sh)
            L.append(f"- Filter value: planned minus shadow = {gap:+.2f}R. " +
                     ("The score filter is NOT adding value." if gap <= 0.1 else "The score filter appears to add value."))
    else:
        L.append("- Skipped (needs Alpaca data).")

    # 5b. Random baseline
    L.append("\n### 5b. Random baseline (control group)")
    ctrl = [c for c in D.read_jsonl(D.CONTROL) if c.get("rules_version", rv) == rv]
    ctrl_res = [c["result_R"] for c in ctrl if c.get("status") == "resolved" and c.get("result_R") is not None]
    L.append(f"- Control entries logged: {len(ctrl)} ({len(ctrl_res)} resolved, {len(ctrl) - len(ctrl_res)} still open). "
             "Same universe, entry, stop/target rules, 10-day exit and hard gates as the real trades; random symbol and direction; score ignored.")
    cs = stats_r(ctrl_res)
    real_rs = [t["result_R"] for t in closed if t.get("result_R") is not None]
    rs_ = stats_r(real_rs)
    if cs:
        L.append(f"- Random controls: {cs['n']} resolved, {cs['avg_R']:+.2f}R avg, win {cs['win_rate']:.0%}")
    if rs_:
        L.append(f"- Real trades (actual fills): {rs_['n']} closed, {rs_['avg_R']:+.2f}R avg, win {rs_['win_rate']:.0%}")
    if rs_ and rs_["n"] >= C.MIN_REAL and len(ctrl_res) >= C.MIN_CONTROL:
        pc = C.percentile_vs_random(rs_["avg_R"], ctrl_res, rs_["n"])
        L.append(f"- Real average R beats {pc:.0%} of random draws of the same size. Verdict: {C.verdict(pc)}")
    else:
        L.append(f"- Verdict withheld: needs {C.MIN_REAL}+ closed real trades (have {len(real_rs)}) and "
                 f"{C.MIN_CONTROL}+ resolved controls (have {len(ctrl_res)}). Numbers above are for tracking only.")
    pl = groups["planned"]
    if len(pl) >= C.MIN_REAL and len(ctrl_res) >= C.MIN_CONTROL:
        pc2 = C.percentile_vs_random(sum(pl) / len(pl), ctrl_res, len(pl))
        L.append(f"- Larger-sample check (all planned candidates, simulated like the controls): {sum(pl)/len(pl):+.2f}R avg "
                 f"beats {pc2:.0%} of random draws. Verdict: {C.verdict(pc2)}")
    L.append("- Caveats: controls assume a fill at the logged entry and exit on the first stop/target touch in daily bars "
             "(gaps fill at the open); real trades enter intraday; one stretch of market is not every regime; "
             "a pass here is evidence, not proof.")

    # 6. Calibration
    L.append("\n### 6. Calibration of p_target")
    cal = [(t["p_target"], 1 if t["exit_reason"] == "target" else 0) for t in closed if t.get("p_target") is not None]
    if cal:
        brier = sum((p - o) ** 2 for p, o in cal) / len(cal)
        L.append(f"- Brier score {brier:.3f} on {len(cal)} trades (0.25 = coin-flip guessing; lower is better)")
        for name, lo, hi in (("<0.40", 0, 0.4), ("0.40-0.55", 0.4, 0.55), (">0.55", 0.55, 1.01)):
            b_ = [o for p, o in cal if lo <= p < hi]
            L.append(f"- p_target {name}: " + (f"{len(b_)} trades, hit target {sum(b_)/len(b_):.0%}" if b_ else "none"))
    else:
        L.append("- No closed trades with p_target yet.")

    # 7. Rule adherence
    L.append("\n### 7. Rule adherence")
    issues = []
    placed = {(t["date_open"], t["symbol"]) for t in trades}
    for c in cands:
        if c["action"] == "planned" and (c["date"], c["symbol"]) not in placed:
            issues.append(f"{c['date']} {c['symbol']}: planned but never placed (a skipped valid trade)")
    for t in trades:
        ok_tier = (t["tier"] == "A" and t["score"] >= 7) or (t["tier"] == "B" and (5 <= t["score"] <= 6 or (t.get("quota") and t["score"] >= 4)))
        if not ok_tier:
            issues.append(f"{t['trade_id']}: tier {t['tier']} does not match score {t['score']}")
        if t.get("risk_dollars", 0) > start_equity * D.RISK_PCT.get(t["tier"], 0) * 1.15:
            issues.append(f"{t['trade_id']}: risk ${t['risk_dollars']} above the tier limit")
        for k in ("thesis", "invalidation", "dissent", "p_target"):
            if not t.get(k):
                issues.append(f"{t['trade_id']}: missing {k}")
    L += [f"- {i}" for i in issues] or ["- No violations found."]

    L.append("\n### 8. Proposed rule changes")
    L.append("- (Socrates: list proposals here with reasoning. Do NOT apply. Changes need 40 closed A+B trades, Chris's approval, and a version bump.)")

    report = "\n".join(L) + "\n"
    with open(D.WEEKLY, "a") as f:
        f.write(report)
    D.REPORTS.mkdir(parents=True, exist_ok=True)
    (D.REPORTS / f"weekly_{today}.md").write_text(report)

    if not offline and start:
        _tearsheet(start)
    return report


def _tearsheet(start):
    """Optional: quantstats HTML report vs SPY, if the library is installed."""
    try:
        import quantstats as qs
    except ImportError:
        return
    try:
        h = D.broker().portfolio_history("1A")
        eq = pd.Series(h["equity"], index=pd.to_datetime(h["timestamp"], unit="s")).dropna()
        eq = eq[eq.index >= pd.Timestamp(start["date"])]
        if len(eq) > 5:
            qs.reports.html(eq.pct_change().dropna(), benchmark="SPY",
                            output=str(D.REPORTS / f"tearsheet_{D.today_str()}.html"), title="Socrates paper desk")
    except Exception:
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    print(run(ap.parse_args().offline))
