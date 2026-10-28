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
ALPHA = 0.05         # two-sided significance level for the verdict


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


def week_of(date_str):
    y, w, _ = dt.date.fromisoformat(date_str).isocalendar()
    return f"{y}-W{w:02d}"


def perm_two_sample(a, b, n_perm=10000, seed=0):
    """Is mean(a) different from mean(b)? a and b are lists of (block, R). Labels are shuffled WITHIN each block
    (ISO week), so trades that moved together in one week can't masquerade as independent evidence.
    Returns (difference_in_means, two_sided_p). p uses the (k+1)/(n+1) rule, so it is never exactly 0."""
    if not a or not b:
        return 0.0, 1.0
    obs = sum(r for _, r in a) / len(a) - sum(r for _, r in b) / len(b)
    blocks = {}
    for lab, rows in ((1, a), (0, b)):
        for blk, r in rows:
            blocks.setdefault(blk, []).append((lab, r))
    na = len(a)
    rng = random.Random(seed)
    hits = 0
    for _ in range(n_perm):
        sa = sb = 0.0
        for items in blocks.values():
            labs = [lab for lab, _ in items]
            rng.shuffle(labs)
            for lab, (_, r) in zip(labs, items):
                if lab:
                    sa += r
                else:
                    sb += r
        diff = sa / na - sb / (len(a) + len(b) - na)
        if abs(diff) >= abs(obs) - 1e-12:
            hits += 1
    return obs, (hits + 1) / (n_perm + 1)


def verdict(diff, p):
    if p < ALPHA and diff > 0:
        return f"better than random (two-sided p = {p:.3f} < {ALPHA}): evidence of skill; confirm it holds in other market conditions."
    if p < ALPHA and diff < 0:
        return f"worse than random (two-sided p = {p:.3f} < {ALPHA}): the process is hurting results."
    return f"not distinguishable from random (two-sided p = {p:.3f}): no evidence of skill yet."
