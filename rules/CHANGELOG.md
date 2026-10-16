# Rules changelog

Trading rules are v1.0 and locked until 40 closed A+B trades. Entries below are corrections made before the first live
trade, or changes to logging/measurement that do not affect which trades are taken.

- 2026-10-14 (pre-launch): added a hard gate: R:R below 1.0 never trades.
- 2026-10-14 (pre-launch): profit target must be at least 1R away (nearer levels are ignored); with none, default 2R; cap 3R.
- 2026-10-16 (measurement only): p_target must stay within 15 points of the neutral base rate 1/(1+R:R); place.py enforces it.
  Reason: first batch logged ~0.50 on 2R targets (base rate ~0.33), which would make calibration data meaningless.
  Does not change which trades are taken.
- 2026-10-16 (measurement only): random control group (5 per DECISION run) and score-vs-forward-R study added to the review.
