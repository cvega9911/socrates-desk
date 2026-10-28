# Rules changelog

Trading rules are v1.0 and locked until 40 closed A+B trades. Entries below are corrections made before the first live
trade, or changes to logging/measurement that do not affect which trades are taken.

- 2026-10-14 (pre-launch): added a hard gate: R:R below 1.0 never trades.
- 2026-10-14 (pre-launch): profit target must be at least 1R away (nearer levels are ignored); with none, default 2R; cap 3R.
- 2026-10-16 (measurement only): p_target must stay within 15 points of the neutral base rate 1/(1+R:R); place.py enforces it.
  Reason: first batch logged ~0.50 on 2R targets (base rate ~0.33), which would make calibration data meaningless.
  Does not change which trades are taken.
- 2026-10-16 (measurement only): random control group (5 per DECISION run) and score-vs-forward-R study added to the review.
- 2026-10-28 (bug fixes and measurement; the trading rules themselves are unchanged, still v1.0):
  - Earnings blackout now checks each stock separately and counts a stock as verified only if its own lookup
    answered. The old single market-wide query could come back incomplete and still count as "verified", so during
    mega-cap earnings week (10-26/27) no stock was blacked out. This enforces the existing universe rule.
  - DECISION and placement only run on real market sessions while the market is open; the time stop only fires
    while the market is open. This enforces the existing schedule (no trading on stale prices on holidays).
  - Free text reaches place.py through an order file, not the shell. Shell expansion had already changed one
    thesis ("$105.92" logged as "05.92" on 10-16); that row is left as logged.
  - Partial fills, rejected orders, failed/timed-out submissions and failed time-stop closes are now handled and
    journaled instead of being reported as placed or left unprotected.
  - Controls, the shadow book and the score study are simulated on split-only bars (not dividend-adjusted), with
    splits after the logged date scaled out. The 5 controls resolved before this date used dividend-adjusted bars.
  - Controls skip stocks whose earnings date could not be verified; a failed shortable lookup aborts the day's
    draw (backfill later) instead of logging a long-only draw.
  - Review: the primary test is now planned candidates vs random controls (both simulated the same way), a
    two-sided block permutation test by ISO week at alpha 0.05 with 20 planned / 50 controls / 10 decision days;
    every other table is labelled exploratory. Calibration leaves out time-stop exits and pre-base-rate trades.
    "This week" is exactly 7 days. Re-running the review replaces that day's section.
  - Anti-tampering: rules_version.txt must match the version in the code; DECISION can't be silently rerun
    (--force is logged); exclusions need a stock and a recorded reason; every candidate row records which regime
    file scored it.

