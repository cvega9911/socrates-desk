"""Random control group (the baseline the real strategy has to beat).

Every DECISION run automatically draws 5 random trades from the same universe: random symbol, random
direction, same entry rule, same stop/target rule, same 10-day horizon, same hard gates (earnings blackout,
R:R >= 1.0, shortable). They ignore the score. They are never traded; they are simulated forward on daily bars
and resolved by sync.py. The weekly review then asks: do the scored, real trades beat random ones?

The draw is seeded by date + rules version, so it is reproducible and cannot be cherry-picked.
"""
import datetime as dt
import random

import desk as D

N_PER_DAY = 5
MIN_REAL = 20        # closed real trades needed before the verdict is shown
MIN_CONTROL = 50     # resolved controls needed before the verdict is shown


def draw(both, blackout, broker, today, verified, backfill=False):
    """both = {symbol: {"long": cand, "short": cand}} built by score.py. Returns (rows, note)."""
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
        direction = rng.choice(["long", "short"])
        c = both[s][direction]
        if c["rr"] < D.MIN_RR:
            continue
        if direction == "short":
            try:
                a = broker.asset(s)
                if not (a.get("shortable") and a.get("easy_to_borrow")):
                    continue
            except Exception:
                continue
        rows.append({"date": today, "rules_version": rv, "symbol": s, "direction": direction,
                     "entry": c["entry"], "stop": c["stop"], "target": c["target"], "rr": c["rr"],
                     "earnings_verified": bool(verified), "backfill": bool(backfill), "status": "open",
                     "result_R": None, "exit_reason": None, "date_resolved": None})
    D.append_jsonl(D.CONTROL, rows)
    return rows, f"{len(rows)} drawn"


def resolve(broker, today):
    """Simulate open controls forward on completed daily bars; mark resolved ones. Returns the newly resolved."""
    rows = D.read_jsonl(D.CONTROL)
    todo = [r for r in rows if r.get("status") == "open" and r["date"] < today]
    if not todo:
        return []
    syms = sorted({r["symbol"] for r in todo})
    first = min(r["date"] for r in todo)
    start = (dt.date.fromisoformat(first) - dt.timedelta(days=3)).isoformat() + "T00:00:00Z"
    end = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=16)).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw = broker.daily_bars(syms, start, end)
    today_d = dt.date.fromisoformat(today)
    frames = {s: D.completed(D.bars_to_df(raw.get(s, [])), today_d) for s in syms}
    done = []
    for r in todo:
        df = frames.get(r["symbol"])
        if df is None or df.empty:
            continue
        out = D.simulate_path(df, r["direction"], r["entry"], r["stop"], r["target"], dt.date.fromisoformat(r["date"]))
        if out is not None:
            r.update({"status": "resolved", "result_R": round(out[0], 3), "exit_reason": out[1], "date_resolved": today})
            done.append(r)
    if done:
        D.write_jsonl(D.CONTROL, rows)
    return done


def percentile_vs_random(real_mean, pool, n, draws=10000, seed=0):
    """Share of random same-size samples from the control pool whose mean R is below real_mean."""
    rng = random.Random(seed)
    below = 0
    for _ in range(draws):
        if sum(rng.choice(pool) for _ in range(n)) / n < real_mean:
            below += 1
    return below / draws


def verdict(p):
    if p >= 0.95:
        return "better than 95%+ of random draws: evidence of skill, but confirm it holds in different market conditions."
    if p <= 0.05:
        return "worse than 95%+ of random draws: the process is hurting results."
    return "within the range of random draws: no evidence of skill yet."
