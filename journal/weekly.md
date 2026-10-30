# Weekly reviews

## Weekly review 2026-10-16 (rules v1.0)

**Sample too small for conclusions** (0 closed trades; need 20 to interpret, 40 to change rules).

### 1. Activity
- This week: 5 opened (A 5, B 0, quota 0), 0 closed
- To date: 5 placed (A 5, B 0, quota 0), 0 closed, 0 unfilled, 5 still open/pending

### 2. Performance to date
- No closed trades yet.

### 3. Benchmark vs SPY
- Since 2026-10-16: account 0.0% vs SPY -1.1% (ahead by 1.1%)
- Note: the account is mostly cash and mixes longs with shorts, so this line mostly reflects market exposure, not skill. Judge the edge on R and on the random baseline (5b), not this line.

### 4. Score buckets (closed trades)
- Score 4: none
- Score 5: none
- Score 6: none
- Score 7: none
- Score 8+: none

### 5. Shadow book (hypothetical, 10-day horizon, same-day ties counted as stops)
- planned: none resolved yet
- blocked: none resolved yet
- shadow: none resolved yet

### 5b. Random baseline (control group)
- Control entries logged: 0 (0 resolved, 0 still open). Same universe, entry, stop/target rules, 10-day exit and hard gates as the real trades; random symbol and direction; score ignored.
- Verdict withheld: needs 20+ closed real trades (have 0) and 50+ resolved controls (have 0). Numbers above are for tracking only.
- Caveats: controls assume a fill at the logged entry and exit on the first stop/target touch in daily bars (gaps fill at the open); real trades enter intraday; one stretch of market is not every regime; a pass here is evidence, not proof.

### 6. Calibration of p_target
- No closed trades with p_target yet.

### 7. Rule adherence
- No violations found.

### 8. Proposed rule changes
- (Socrates: list proposals here with reasoning. Do NOT apply. Changes need 40 closed A+B trades, Chris's approval, and a version bump.)

## Weekly review 2026-10-23 (rules v1.0)

**Sample too small for conclusions** (0 closed trades; need 20 to interpret, 40 to change rules).

### 1. Activity
- This week: 5 opened (A 5, B 0, quota 0), 0 closed
- To date: 5 placed (A 5, B 0, quota 0), 0 closed, 0 unfilled, 5 still open/pending

### 2. Performance to date
- No closed trades yet.

### 3. Benchmark vs SPY
- Since 2026-10-16: account 0.4% vs SPY -1.5% (ahead by 2.0%)
- Note: the account is mostly cash and mixes longs with shorts, so this line mostly reflects market exposure, not skill. Judge the edge on R and on the random baseline (5b), not this line.

### 4. Score buckets (closed trades)
- Score 4: none
- Score 5: none
- Score 6: none
- Score 7: none
- Score 8+: none

### 5. Shadow book (hypothetical, 10-day horizon, same-day ties counted as stops)
- planned: none resolved yet
- blocked: none resolved yet
- shadow: none resolved yet

### 5b. Random baseline (control group)
- Control entries logged: 25 (2 resolved, 23 still open). Same universe, entry, stop/target rules, 10-day exit and hard gates as the real trades; random symbol and direction; score ignored.
- Random controls (all resolved): 2, -1.00R avg, win 0%
- Verdict withheld. Only trades and controls opened 15+ days ago count. Matured so far: 0 real (need 20) and 0 controls (need 50). Numbers above are for tracking only.
- Caveats: controls assume a fill at the logged entry and exit on the first stop/target touch in daily bars (gaps fill at the open); real trades enter intraday; one stretch of market is not every regime; a pass here is evidence, not proof.

### 5c. Does the score predict forward R? (all scored candidates, matured only)
- No matured candidates yet (each needs 10 trading days of data after the decision).

### 6. Calibration of p_target (anchored to the neutral base rate 1 / (1 + R:R))
- No closed trades with p_target yet.

### 7. Rule adherence
- No violations found.

### 8. Proposed rule changes
- (Socrates: list proposals here with reasoning. Do NOT apply. Changes need 40 closed A+B trades, Chris's approval, and a version bump.)

## Weekly review 2026-10-30 (rules v1.0)

**Sample too small for conclusions** (5 closed trades; need 20 to interpret, 40 to change rules).

### 1. Activity
- This week: 1 opened (A 1, B 0, quota 0), 5 closed
- To date: 6 placed (A 6, B 0, quota 0), 5 closed, 0 unfilled, 1 still open/pending, 0 rejected or failed to submit

### 2. Performance to date (real fills)
- Win rate 80.0% | expectancy (avg R per trade) +0.46R | total P/L $1,121.27 | realized max drawdown -0.5% (closed trades only)
- Tier A: 5 trades, win 80.0%, +0.46R avg

### 3. Benchmark vs SPY
- Since 2026-10-16: account 1.3% vs SPY price -0.3% (ahead by 1.6%)
- Note: the account is mostly cash and mixes longs with shorts, so this line mostly reflects market exposure, not skill. Judge the edge on R and on the random baseline (5b), not this line.

### 4. Score buckets (closed trades)
- Score 4: none
- Score 5: none
- Score 6: none
- Score 7: none
- Score 8+: 5 trades, +0.46R avg, win 80.0%

### 5. Shadow book (simulated, 10-day horizon, same-day ties counted as stops) [exploratory]
- planned: none matured yet
- blocked: none matured yet
- shadow: none matured yet

### 5b. Random baseline (control group) [PRIMARY TEST]
- Control entries logged: 50 (14 resolved, 36 still open). Same universe, entry, stop/target rules, 10-day exit and hard gates as the real trades; random symbol and direction; score ignored.
- Random controls (all resolved): 14, -0.42R avg, win 21%
- Real trades (actual fills, descriptive only): 5, +0.46R avg, win 80%
- Verdict withheld: the test is read only at 13, 26, 52 closed fortnights (fixed in advance). Closed fortnights with planned and control data so far: 0. Numbers above are for tracking only.
- Caveats: simulations assume a fill at the logged entry and exit on the first stop/target touch in daily bars (gaps fill at the open); each planned trade is compared with same-day, same-direction controls and the test treats fortnights as independent, which is close but not exact; one stretch of market is not every regime; a pass here is evidence, not proof.

### 5c. Does the score predict forward R? (all scored candidates, matured only) [exploratory]
- No matured candidates yet (each needs 10 trading days of data after the decision).

### 6. Calibration of p_target (anchored to the neutral base rate 1 / (1 + R:R))
- No closed trades that ended at the target or stop yet.
- Left out: 0 time-stop exits (neither target nor stop was hit) and 5 trades placed before the base-rate rule (2026-10-16).
- Note: the base rate ignores the 10-day time stop, so the true target-before-stop rate runs a little below it.

### 7. Rule adherence
- 2026-10-29 XOM: excluded by Socrates \(excluded today: earnings 2026-10-30 before open \(thinkmarkets Q4 2026 calendar, checked 2026-10-29\) — ~1 trading day, inside window\)
- 2026-10-29 AAPL: excluded by Socrates \(excluded today: earnings 2026-11-02 after close \(thinkmarkets Q4 2026 calendar, checked 2026-10-29\) — ~4 trading days, inside window\)
- 2026-10-29 AMZN: excluded by Socrates \(no reason\)
- 2026-10-30 AAPL: excluded by Socrates \(excluded today: earnings 2026-11-02 after close \(apple.com IR + trefis.com 10/30, checked 2026-10-30\) — ~3 trading days, inside window\)

### 8. Proposed rule changes
- (Socrates: list proposals here with reasoning. Do NOT apply. Changes need 40 closed A+B trades, Chris's approval, and a version bump.)
