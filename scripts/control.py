"""Random control group (the baseline the real strategy has to beat).

Every DECISION run automatically draws 5 random trades from the same universe: random symbol, random
direction, same entry rule, same stop/target rule, same 10-day horizon, same hard gates (earnings blackout,
R:R >= 1.0, shortable). They ignore the score. They are never traded; they are simulated forward on daily bars
and resolved by sync.py. The weekly review then asks: do the scored candidates beat random ones?

The draw is seeded by date + rules version, so it is reproducible on the same Python version (the version is
logged on every row). The only way to draw on a day after the fact is `score.py --mode controls`, which is
logged as backfill=true and still produces the same seeded draw.
"""
import datetime as dt
import platform
import random

import desk as D

N_PER_DAY = 5
MIN_REAL = 20        # matured real trades / planned candidates needed before a verdict is shown
MIN_CONTROL = 50     # matured controls needed before a verdict is shown
MIN_DAYS = 10        # distinct decision days needed: trades opened the same day move together
ALPHA = 0.05         # significance level for a "worse than random" verdict
ALPHA_BETTER = 0.04  # slightly stricter for "better than random": the daily-bar simulation favours the plan's wider,
                     # trend-placed stops by about +0.02R even with no skill, so 0.04 keeps false "skill" calls at ~2.5%
                     # (checked on 9,000 simulated no-skill runs in flat, rising and regime-switching markets)


def draw(both, blackout, broker, today, verified, backfill=False):
    """both = {symbol: {"long": cand, "short": cand}} built by score.py; verified = stocks whose earnings date was
    checked. Returns (rows, note). Raises if a shortable lookup fails, so a partial (long-only) draw is never
    logged; the day can be backfilled later with score.py --mode controls."""
    rv = D.rules_version()
    if any(r.get("date") == today and r.get("rules_version") == rv for r in D.read_jsonl(D.CONTROL)):
        return [], "controls for today already drawn"
    rng = random.Random(f"{today}|{rv}|control")
    symbols = sorted(both)
    rng.shuffle(symbols)
    rows = []
    for s in symbols:
        if len(rows) >= N_PER_DAY:
            break
        if s in blackout:
            continue
        if s in D.STOCKS and s not in verified:
            continue        # a real trade would need a manual earnings check; a control can't do one, so skip it
        direction = rng.choice(["long", "short"])
        c = both[s][direction]
        if c["rr"] < D.MIN_RR:
            continue
        if direction == "short":
            a = broker.asset(s)          # errors propagate: never log a draw that silently lost its shorts
            if not (a.get("shortable") and a.get("easy_to_borrow")):
                continue
        rows.append({"date": today, "rules_version": rv, "symbol": s, "direction": direction,
                     "entry": c["entry"], "stop": c["stop"], "target": c["target"], "rr": c["rr"],
                     "earnings_verified": s not in D.STOCKS or s in verified, "backfill": bool(backfill),
                     "py": platform.python_version(), "status": "open",
                     "result_R": None, "exit_reason": None, "date_resolved": None})
    D.append_jsonl(D.CONTROL, rows)
    return rows, f"{len(rows)} drawn"


def resolve(broker, today):
    """Simulate open controls forward on completed daily bars; mark resolved ones. Returns the newly resolved."""
    with D.journal_lock():
        rows = D.read_jsonl(D.CONTROL)
        todo = [r for r in rows if r.get("status") == "open" and r["date"] < today]
        if not todo:
            return []
        frames = D.simulation_bars(broker, sorted({r["symbol"] for r in todo}), min(r["date"] for r in todo), today)
        done = []
        for r in todo:
            out = D.simulate_logged(frames.get(r["symbol"]), r["direction"], r["entry"], r["stop"], r["target"],
                                    dt.date.fromisoformat(r["date"]))
            if out is not None:
                r.update({"status": "resolved", "result_R": round(out[0], 3), "exit_reason": out[1], "date_resolved": today})
                done.append(r)
        if done:
            D.write_jsonl(D.CONTROL, rows)
        return done


BLOCK_DAYS = 14                  # >= the 10-trading-day horizon, so outcomes in different blocks barely overlap
BLOCK_EPOCH = dt.date(2026, 1, 5)    # a Monday; blocks are fixed calendar fortnights
MIN_BLOCKS = 12


def week_of(date_str):
    y, w, _ = dt.date.fromisoformat(date_str).isocalendar()
    return f"{y}-W{w:02d}"


def block_of(date_str):
    return (dt.date.fromisoformat(date_str) - BLOCK_EPOCH).days // BLOCK_DAYS


def _mean(xs):
    return sum(xs) / len(xs)


def block_diffs(a, b):
    """a, b: lists of (date, direction, R, ...). Each a-row is compared with the b-rows from the SAME decision day and
    the SAME direction (its R minus their mean), so neither the market's direction nor that day's move can pose as
    skill. Those differences are averaged per fortnight; fortnights are the independent units for the test.
    Rows with no same-day, same-direction match are skipped."""
    ref = {}
    for row in b:
        ref.setdefault((row[0], row[1]), []).append(row[2])
    blocks = {}
    for row in a:
        m = ref.get((row[0], row[1]))
        if m:
            blocks.setdefault(block_of(row[0]), []).append(row[2] - _mean(m))
    return [_mean(v) for _, v in sorted(blocks.items())]


def signflip_test(diffs, n_perm=10000, seed=0):
    """Two-sided sign-flip permutation test on per-fortnight differences (blocks are treated as the independent
    units). Returns (mean_difference, p); p uses (k+1)/(n+1), so it is never exactly 0."""
    if not diffs:
        return 0.0, 1.0
    obs = _mean(diffs)
    rng = random.Random(seed)
    hits = 0
    for _ in range(n_perm):
        if abs(sum(x if rng.random() < 0.5 else -x for x in diffs) / len(diffs)) >= abs(obs) - 1e-12:
            hits += 1
    return obs, (hits + 1) / (n_perm + 1)


def compare(a, b, n_perm=10000, seed=0):
    """(mean_difference, p, n_blocks) for a vs b, direction-matched and blocked by fortnight."""
    d = block_diffs(a, b)
    diff, p = signflip_test(d, n_perm, seed)
    return diff, p, len(d)


def verdict(diff, p):
    if p < ALPHA_BETTER and diff > 0:
        return f"better than random (p = {p:.3f} < {ALPHA_BETTER}): evidence of skill; confirm it holds in other market conditions."
    if p < ALPHA and diff < 0:
        return f"worse than random (two-sided p = {p:.3f} < {ALPHA}): the process is hurting results."
    return f"not distinguishable from random (two-sided p = {p:.3f}): no evidence of skill yet."
