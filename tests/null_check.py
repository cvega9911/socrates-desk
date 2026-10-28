"""How often does the primary test make a false call when there is no skill at all? Run by hand (not in CI):
    python tests/null_check.py            # about a minute
It simulates whole experiments (52 fortnights, a few planned trades and five controls per decision day, a shared
market move per day) with NO edge, reads the test exactly the way review.py does at each pre-registered look,
and counts experiments where any look says "better" or "worse". Target: about 2.5% each way or less.
--lean adds a fixed per-trade bias in R to the planned side (the daily-bar simulator's known ~+0.02R lean)."""
import argparse
import datetime as dt
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import control as C  # noqa: E402


def experiment(rng, lean, n_fortnights=52, per_day=2):
    days, d = [], C.BLOCK_EPOCH
    end = C.BLOCK_EPOCH + dt.timedelta(days=C.BLOCK_DAYS * n_fortnights)
    while d < end:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += dt.timedelta(days=1)
    a, b = [], []
    for day in days:
        m = rng.gauss(0, 1)
        for _ in range(per_day):
            s = 1 if rng.random() < 0.7 else -1
            a.append((day, "long" if s > 0 else "short", s * m + lean + rng.gauss(0, 1)))
        for _ in range(C.N_PER_DAY):
            s = rng.choice((1, -1))
            b.append((day, "long" if s > 0 else "short", s * m + rng.gauss(0, 1)))
    today = (end + dt.timedelta(days=C.SETTLE_DAYS + 1)).isoformat()
    calls = set()
    for k in C.LOOKS:          # every look, as the weekly review would reach them in turn
        cut = (C.BLOCK_EPOCH + dt.timedelta(days=C.BLOCK_DAYS * k + C.SETTLE_DAYS)).isoformat()
        look, info = C.primary_look(a, b, min(cut, today), n_perm=1000)
        if look:
            v = C.verdict(info["diff"], info["p"])
            calls |= {"better"} if v.startswith("better") else {"worse"} if v.startswith("worse") else set()
    return calls


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=400)
    ap.add_argument("--lean", type=float, default=0.0)
    a = ap.parse_args()
    rng = random.Random(2026)
    better = worse = 0
    for _ in range(a.runs):
        c = experiment(rng, a.lean)
        better += "better" in c
        worse += "worse" in c
    print(f"{a.runs} no-skill experiments, lean {a.lean:+.2f}R: false 'better' {better / a.runs:.1%}, "
          f"false 'worse' {worse / a.runs:.1%}")


if __name__ == "__main__":
    main()
