"""Weekly review for rules v1.0. Writes the report to reports/weekly_<date>.md and journal/weekly.md
(re-running on the same day replaces that day's section instead of adding a second copy).

Usage:
  python scripts/review.py              # full review (needs Alpaca for benchmark + simulations)
  python scripts/review.py --offline    # journal-only stats, no Alpaca calls

Statistics, stated up front so nobody goes fishing:
- PRIMARY test (5b): do the planned candidates beat the random controls? Both are simulated the same way from the
  same logged levels, so fills, slippage and timing can't tilt the comparison. Two-sided block permutation test
  (labels shuffled within each ISO week), alpha 0.05, only after 20 planned + 50 controls over 10+ decision days.
- Everything else (real fills vs controls, planned vs shadow, the score study, factor tables) is EXPLORATORY:
  no multiple-comparison correction, so treat a "significant" result there as a lead, not a finding.
"""
import argparse
import datetime as dt
import json
import random
import re

import pandas as pd

import control as C
import desk as D


def pct(x):
    return "n/a" if x is None else f"{x:.1%}"


def stats_r(rs):
    rs = [r for r in rs if r is not None]
    if not rs:
        return None
    return {"n": len(rs), "win_rate": sum(r > 0 for r in rs) / len(rs), "avg_R": sum(rs) / len(rs)}


def stats(rows):
    return stats_r([t.get("result_R") for t in rows]) or {"n": 0}


def corr(xs, ys):
    n = len(xs)
    if n < 3:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    sx = sum((x - mx) ** 2 for x in xs) ** 0.5
    sy = sum((y - my) ** 2 for y in ys) ** 0.5
    if sx == 0 or sy == 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def within_day_perm_p(rows, n_perm=2000, seed=0):
    """Does the score predict forward R beyond what the market did that day?
    Correlation of score vs R, with a one-sided p-value from shuffling scores among the candidates of the SAME day.
    Uses (k+1)/(n+1), so p is never reported as exactly 0."""
    scores, rs = [r["score"] for r in rows], [r["R"] for r in rows]
    obs = corr(scores, rs)
    by_day = {}
    for i, r in enumerate(rows):
        by_day.setdefault(r["date"], []).append(i)
    rng = random.Random(seed)
    ge = 0
    for _ in range(n_perm):
        sh = scores[:]
        for idx in by_day.values():
            vals = [sh[i] for i in idx]
            rng.shuffle(vals)
            for i, v in zip(idx, vals):
                sh[i] = v
        if corr(sh, rs) >= obs:
            ge += 1
    return obs, (ge + 1) / (n_perm + 1)


def latest_runs(cands):
    """For each decision day keep only the rows of the run whose plan was actually used (the last one)."""
    def run_n(c):
        m = re.search(r"rerun(\d+)$", str(c.get("run", "")))
        return int(m.group(1)) if m else 0
    last = {}
    for c in cands:
        last[c["date"]] = max(last.get(c["date"], 0), run_n(c))
    return [c for c in cands if run_n(c) == last[c["date"]]]


def simulate_cands(cands, frames):
    """Simulate each (date, symbol) candidate once; matured only. Returns rows with date, action, score, breakdown, R."""
    out, seen = [], set()
    for c in cands:
        key = (c["date"], c["symbol"])
        fr = frames.get(c["symbol"])
        if key in seen or not fr:
            continue
        seen.add(key)
        d0 = dt.date.fromisoformat(c["date"])
        if not D.matured(fr[0], d0):
            continue        # not matured: early exits would bias the sample
        res = D.simulate_logged(fr, c["direction"], c["entry"], c["stop"], c["target"], d0)
        if res is not None:
            out.append({"date": c["date"], "action": c.get("action"), "score": c["score"],
                        "breakdown": c.get("breakdown") or {}, "R": res[0]})
    return out


def score_study(cands, frames):
    """Kept for compatibility: every scored candidate simulated forward, matured only."""
    return simulate_cands(cands, frames)


def max_drawdown(closed, start_equity):
    """Realized drawdown from closed trades only (open positions are not marked to market here)."""
    eq, peak, mdd = start_equity, start_equity, 0.0
    for t in sorted(closed, key=lambda t: t["date_close"]):
        eq += t.get("pnl") or 0
        peak = max(peak, eq)
        mdd = min(mdd, (eq - peak) / peak)
    return mdd


def verify_against_broker(trades):
    """Cross-check closed trades against Alpaca's own order records, so an edited journal can't hide a result."""
    out = []
    try:
        b = D.broker()
    except Exception as e:
        return [f"could not check the journal against Alpaca ({type(e).__name__})"]
    for t in trades:
        if t.get("status") != "closed" or not t.get("order_id"):
            continue
        try:
            o = b.order(t["order_id"])
            fill = float(o.get("filled_avg_price") or 0)
            if fill and t.get("entry_fill") and abs(fill - float(t["entry_fill"])) > 0.01 and not t.get("partial_fill"):
                out.append(f"{t['trade_id']}: journal entry {t['entry_fill']} != Alpaca fill {fill}")
            exits = list(o.get("legs") or [])
            for k in ("exit_order_id", "close_order_id"):
                if t.get(k):
                    e = b.order(t[k])
                    exits += [e] + list(e.get("legs") or [])
            prices = [float(x["filled_avg_price"]) for x in exits
                      if x.get("status") == "filled" and x.get("filled_avg_price")]
            if prices and not any(abs(p - float(t["exit_price"])) <= 0.01 for p in prices):
                out.append(f"{t['trade_id']}: journal exit {t['exit_price']} matches no Alpaca exit fill {prices}")
        except Exception as e:
            out.append(f"{t['trade_id']}: could not check against Alpaca ({type(e).__name__})")
    return out


def write_weekly(today, report):
    """Replace today's section in journal/weekly.md if it exists, otherwise append it."""
    D.JOURNAL.mkdir(parents=True, exist_ok=True)
    old = D.WEEKLY.read_text() if D.WEEKLY.exists() else ""
    head = f"\n## Weekly review {today} "
    i = old.find(head)
    if i >= 0:
        j = old.find("\n## Weekly review ", i + len(head))
        new = old[:i] + report.rstrip("\n") + "\n" + (old[j:] if j >= 0 else "")
    else:
        new = old + report
    D.atomic_write(D.WEEKLY, new)


def run(offline=False):
    with D.journal_lock():
        return _run(offline)


def _run(offline):
    today = D.today_str()
    today_d = dt.date.fromisoformat(today)
    rv = D.rules_version()
    trades = [t for t in D.read_jsonl(D.TRADES) if t.get("rules_version", rv) == rv]
    all_cands = [c for c in D.read_jsonl(D.CANDIDATES)
                 if str(c.get("run", "")).startswith("decision") and c.get("rules_version", rv) == rv]
    cands = latest_runs(all_cands)
    placed_attempts = [t for t in trades if t.get("status") not in ("failed_submit", "rejected")]
    closed = [t for t in trades if t.get("status") == "closed"]
    week_start = (today_d - dt.timedelta(days=7)).isoformat()      # the 7 days AFTER this date, today included

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
    opened_wk = [t for t in placed_attempts if t["date_open"] > week_start]
    closed_wk = [t for t in closed if (t.get("date_close") or "") > week_start]
    L.append("### 1. Activity")
    L.append(f"- This week: {len(opened_wk)} opened ({by_tier(opened_wk)}), {len(closed_wk)} closed")
    L.append(f"- To date: {len(placed_attempts)} placed ({by_tier(placed_attempts)}), {len(closed)} closed, "
             f"{sum(t['status']=='unfilled' for t in trades)} unfilled, "
             f"{sum(t['status'] in D.ACTIVE for t in trades)} still open/pending, "
             f"{sum(t['status'] in ('failed_submit', 'rejected') for t in trades)} rejected or failed to submit")

    # 2. Performance
    s = stats(closed)
    L.append("\n### 2. Performance to date (real fills)")
    if s["n"]:
        L.append(f"- Win rate {pct(s['win_rate'])} | expectancy (avg R per trade) {s['avg_R']:+.2f}R | "
                 f"total P/L ${sum(t.get('pnl') or 0 for t in closed):,.2f} | realized max drawdown "
                 f"{pct(max_drawdown(closed, start_equity))} (closed trades only)")
        for tier in ("A", "B"):
            st = stats([t for t in closed if t["tier"] == tier])
            if st["n"]:
                L.append(f"- Tier {tier}: {st['n']} trades, win {pct(st['win_rate'])}, {st['avg_R']:+.2f}R avg")
    else:
        L.append("- No closed trades yet.")

    # 3. Benchmark
    L.append("\n### 3. Benchmark vs SPY")
    frames = {}
    if offline or not start:
        L.append("- Skipped (offline or start_date.txt not initialised yet).")
    else:
        b = D.broker()
        equity = float(b.account()["equity"])
        syms = sorted({c["symbol"] for c in cands} | {c["symbol"] for c in D.read_jsonl(D.CONTROL)} | {"SPY"})
        first = min([c["date"] for c in cands] + [c["date"] for c in D.read_jsonl(D.CONTROL)] + [start["date"]])
        frames = D.simulation_bars(b, syms, first, today)
        spy_now = float(frames["SPY"][0]["close"].iloc[-1])
        spy_start = start["spy_close"] / D.split_factor(frames["SPY"][0], frames["SPY"][1],
                                                        dt.date.fromisoformat(start["date"]))
        acct_chg = equity / start_equity - 1
        spy_chg = spy_now / spy_start - 1
        L.append(f"- Since {start['date']}: account {pct(acct_chg)} vs SPY price {pct(spy_chg)} "
                 f"({'ahead' if acct_chg > spy_chg else 'behind'} by {abs(acct_chg - spy_chg):.1%})")
        L.append("- Note: the account is mostly cash and mixes longs with shorts, so this line mostly reflects market exposure, "
                 "not skill. Judge the edge on R and on the random baseline (5b), not this line.")

    # 4. Score buckets
    L.append("\n### 4. Score buckets (closed trades)")
    for label, lo, hi in (("4", 4, 4), ("5", 5, 5), ("6", 6, 6), ("7", 7, 7), ("8+", 8, 10)):
        st = stats([t for t in closed if lo <= t["score"] <= hi])
        L.append(f"- Score {label}: " + (f"{st['n']} trades, {st['avg_R']:+.2f}R avg, win {pct(st['win_rate'])}" if st["n"] else "none"))

    # 5. Shadow book: every logged candidate walked forward from the NEXT daily bar (same simulator as the controls)
    L.append("\n### 5. Shadow book (simulated, 10-day horizon, same-day ties counted as stops) [exploratory]")
    sim = simulate_cands(cands, frames) if frames else []
    groups = {"planned": [], "blocked": [], "shadow": []}
    for r in sim:
        if r["action"] in groups:
            groups[r["action"]].append((C.week_of(r["date"]), r["R"]))
    if frames:
        for g, rs in groups.items():
            st = stats_r([r for _, r in rs])
            L.append(f"- {g}: " + (f"{st['n']} matured, {st['avg_R']:+.2f}R avg, win {st['win_rate']:.0%}" if st else "none matured yet"))
        p, sh = groups["planned"], groups["shadow"]
        if len(p) >= 10 and len(sh) >= 10:
            diff, pv = C.perm_two_sample(p, sh)
            L.append(f"- Planned minus shadow: {diff:+.2f}R (two-sided p = {pv:.3f}). " +
                     ("Exploratory hint that the score filter separates good from bad setups."
                      if pv < C.ALPHA and diff > 0 else "No evidence yet that the score filter adds value."))
    else:
        L.append("- Skipped (needs Alpaca data).")

    # 5b. PRIMARY: planned candidates vs random controls, both simulated identically, matured only.
    L.append("\n### 5b. Random baseline (control group) [PRIMARY TEST]")
    ctrl = [c for c in D.read_jsonl(D.CONTROL) if c.get("rules_version", rv) == rv]
    ctrl_ok = [c for c in ctrl if c.get("status") == "resolved" and c.get("result_R") is not None]
    ctrl_mat = []
    if frames:
        for c in ctrl_ok:
            fr = frames.get(c["symbol"])
            if fr and D.matured(fr[0], dt.date.fromisoformat(c["date"])):
                ctrl_mat.append((C.week_of(c["date"]), c["result_R"]))
    L.append(f"- Control entries logged: {len(ctrl)} ({len(ctrl_ok)} resolved, {len(ctrl) - len(ctrl_ok)} still open). "
             "Same universe, entry, stop/target rules, 10-day exit and hard gates as the real trades; random symbol and direction; score ignored.")
    cs = stats_r([c["result_R"] for c in ctrl_ok])
    if cs:
        L.append(f"- Random controls (all resolved): {cs['n']}, {cs['avg_R']:+.2f}R avg, win {cs['win_rate']:.0%}")
    rs_ = stats_r([t.get("result_R") for t in closed])
    if rs_:
        L.append(f"- Real trades (actual fills, descriptive only): {rs_['n']}, {rs_['avg_R']:+.2f}R avg, win {rs_['win_rate']:.0%}")
    pl = groups["planned"]
    plan_days = len({w for w, _ in pl})
    pl_dates = len({r["date"] for r in sim if r["action"] == "planned"})
    if len(pl) >= C.MIN_REAL and len(ctrl_mat) >= C.MIN_CONTROL and pl_dates >= C.MIN_DAYS:
        diff, pv = C.perm_two_sample(pl, ctrl_mat)
        L.append(f"- Planned candidates ({len(pl)} over {pl_dates} decision days, {plan_days} weeks) vs controls "
                 f"({len(ctrl_mat)}), both simulated the same way: difference {diff:+.2f}R. Verdict: {C.verdict(diff, pv)}")
    else:
        L.append(f"- Verdict withheld. Needs {C.MIN_REAL}+ matured planned candidates over {C.MIN_DAYS}+ decision days and "
                 f"{C.MIN_CONTROL}+ matured controls; have {len(pl)} over {pl_dates} days and {len(ctrl_mat)}. "
                 "Numbers above are for tracking only.")
    L.append("- Caveats: simulations assume a fill at the logged entry and exit on the first stop/target touch in daily bars "
             "(gaps fill at the open); trades opened in the same week move together, which the week-blocked test accounts "
             "for only partly; one stretch of market is not every regime; a pass here is evidence, not proof.")

    # 5c. Does the score predict forward results? Every scored candidate, not just traded ones. Exploratory.
    L.append("\n### 5c. Does the score predict forward R? (all scored candidates, matured only) [exploratory]")
    study = sim
    if not frames:
        L.append("- Skipped (needs Alpaca data).")
    elif not study:
        L.append(f"- No matured candidates yet (each needs {D.TIME_STOP_DAYS} trading days of data after the decision).")
    else:
        days = len({r["date"] for r in study})
        L.append(f"- {len(study)} matured candidate-trades across {days} decision days "
                 f"({len({(c['date'], c['symbol']) for c in cands}) - len(study)} not matured yet).")
        for label, lo, hi in (("10", 10, 10), ("9", 9, 9), ("8", 8, 8), ("7", 7, 7), ("5-6", 5, 6), ("4 or less", 0, 4)):
            b_ = [r["R"] for r in study if lo <= r["score"] <= hi]
            L.append(f"- Score {label}: " + (f"{len(b_)} trades, {sum(b_)/len(b_):+.2f}R avg, win {sum(r > 0 for r in b_)/len(b_):.0%}" if b_ else "none"))
        for factor, pts in (("trend", (0, 1, 2)), ("rs", (0, 1, 2)), ("regime", (0, 1, 2)), ("rr", (0, 1, 2)), ("momentum", (0, 1)), ("volume", (0, 1))):
            parts = []
            for v in pts:
                b_ = [r["R"] for r in study if r["breakdown"].get(factor) == v]
                if b_:
                    parts.append(f"{v} -> {sum(b_)/len(b_):+.2f}R (n={len(b_)})")
            if parts:
                L.append(f"- Factor {factor}: " + "; ".join(parts))
        if len(study) >= 200 and days >= 10:
            r_obs, p_val = within_day_perm_p(study)
            verdict_txt = ("exploratory hint that the score predicts forward R beyond the day's market move"
                           if p_val < C.ALPHA and r_obs > 0 else "no reliable predictive power shown yet")
            L.append(f"- Score vs R correlation {r_obs:+.3f}; within-day permutation p = {p_val:.3f} ({verdict_txt}).")
        else:
            L.append(f"- Significance test withheld: needs 200+ matured candidate-trades over 10+ decision days (have {len(study)} over {days}).")
        L.append("- Caveats: candidates on consecutive days overlap in time, so the effective sample is much smaller than the row "
                 "count and the p-value is optimistic; the rr factor changes the size of winning R by construction; "
                 "the regime rows test Socrates' macro research (regime 2 vs 1 vs 0); several tables are read at once, so "
                 "expect some false leads.")

    # 6. Calibration: p_target = chance the target is hit BEFORE the stop. Trades that ended on the time stop hit
    # neither, so they are left out (counted below), as are trades from before the base-rate rule (no p_base).
    L.append("\n### 6. Calibration of p_target (anchored to the neutral base rate 1 / (1 + R:R))")
    cal, n_time, n_pre = [], 0, 0
    for t in closed:
        if t.get("p_target") is None:
            continue
        if t.get("p_base") is None:
            n_pre += 1
            continue
        if t.get("exit_reason") not in ("target", "stop"):
            n_time += 1
            continue
        cal.append((t["p_target"], t["p_base"], 1 if t["exit_reason"] == "target" else 0))
    if cal:
        bs = sum((p - o) ** 2 for p, b, o in cal) / len(cal)
        bb = sum((b - o) ** 2 for p, b, o in cal) / len(cal)
        skill = (1 - bs / bb) if bb > 0 else 0.0
        L.append(f"- Brier score {bs:.3f} on {len(cal)} trades (lower is better). Reference: always saying the base rate "
                 f"scores {bb:.3f}.")
        L.append(f"- Anchor check: average p_target {sum(p for p, b, o in cal)/len(cal):.2f} vs neutral base rate "
                 f"{sum(b for p, b, o in cal)/len(cal):.2f}; actual target-before-stop rate {sum(o for p, b, o in cal)/len(cal):.0%}. "
                 f"Brier skill vs the base rate: {skill:+.2f} (positive = his adjustments added information; "
                 f"meaningless under ~20 trades)")
        for name, lo, hi in (("< 0.25", 0, 0.25), ("0.25-0.35", 0.25, 0.35), ("> 0.35", 0.35, 1.01)):
            b_ = [o for p, b, o in cal if lo <= p < hi]
            L.append(f"- p_target {name}: " + (f"{len(b_)} trades, hit target {sum(b_)/len(b_):.0%}" if b_ else "none"))
    else:
        L.append("- No closed trades that ended at the target or stop yet.")
    if n_time or n_pre:
        L.append(f"- Left out: {n_time} time-stop exits (neither target nor stop was hit) and {n_pre} trades placed "
                 "before the base-rate rule (2026-10-16).")
    L.append("- Note: the base rate ignores the 10-day time stop, so the true target-before-stop rate runs a little below it.")

    # 7. Rule adherence
    L.append("\n### 7. Rule adherence")
    issues = []
    tried = {(t["date_open"], t["symbol"]) for t in trades}
    for c in cands:
        if c["action"] == "planned" and (c["date"], c["symbol"]) not in tried:
            issues.append(f"{c['date']} {c['symbol']}: planned but never placed (a skipped valid trade)")
    for t in trades:
        if t.get("status") in ("failed_submit", "rejected"):
            issues.append(f"{t['trade_id']}: order {t['status']} ({t.get('exit_reason') or t.get('error', '')})")
            continue
        ok_tier = (t["tier"] == "A" and t["score"] >= 7) or (t["tier"] == "B" and (5 <= t["score"] <= 6 or (t.get("quota") and t["score"] >= 4)))
        if not ok_tier:
            issues.append(f"{t['trade_id']}: tier {t['tier']} does not match score {t['score']}")
        eq = t.get("equity_at_open") or start_equity
        if (t.get("risk_dollars") or 0) > eq * D.RISK_PCT.get(t["tier"], 0) * 1.15:
            issues.append(f"{t['trade_id']}: risk ${t['risk_dollars']} above the tier limit")
        for k in ("thesis", "invalidation", "dissent", "p_target"):
            if not t.get(k):
                issues.append(f"{t['trade_id']}: missing {k}")
        if t.get("text_source") == "cli":
            issues.append(f"{t['trade_id']}: thesis passed on the command line (shell can alter it); use an order file")
        if t.get("partial_fill"):
            issues.append(f"{t['trade_id']}: partial fill ({t['shares']} of {t.get('planned_shares')} sh)")
    reruns = sorted({c["date"] for c in all_cands if "rerun" in str(c.get("run", ""))})
    for d in reruns:
        issues.append(f"{d}: DECISION was rerun with --force (only the last plan counts)")
    for c in cands:
        if c.get("excluded"):
            issues.append(f"{c['date']} {c['symbol']}: excluded by Socrates ({c.get('block_reason') or 'no reason'})")
    if not offline:
        issues += verify_against_broker(trades)
    L += [f"- {D.md_safe(i, 400)}" for i in issues] or ["- No violations found."]

    L.append("\n### 8. Proposed rule changes")
    L.append("- (Socrates: list proposals here with reasoning. Do NOT apply. Changes need 40 closed A+B trades, Chris's approval, and a version bump.)")

    report = "\n".join(L) + "\n"
    write_weekly(today, report)
    D.REPORTS.mkdir(parents=True, exist_ok=True)
    D.atomic_write(D.REPORTS / f"weekly_{today}.md", report)

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
    except Exception as e:
        D.warn(f"tearsheet skipped ({type(e).__name__}: {e})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    print(run(ap.parse_args().offline))
