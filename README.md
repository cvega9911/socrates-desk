# Socrates's Desk

> **Paper trading only, no real money.** A research experiment that tests a rules-based strategy against a random baseline. Results are logged as they happen, including losses. Not financial advice.

A paper-trading research experiment run by Socrates (a locally deployed Hermes agent framework running on Qwen 3.8 27b). Since small models are prone to accuracy issues, the workflow ensures that the scripts do the math and the bookkeeping so that Socrates (or whatever agent you may use) can focus on research and writing the reasoning.

```
socrates-desk/
  rules/      the system prompt (v1.0, read-only) + CHANGELOG.md
  scripts/    score.py, place.py, sync.py, review.py, control.py, commit.sh, secret_scan.sh (+ broker.py, desk.py helpers)
  journal/    candidates.jsonl, trades.jsonl, control.jsonl, weekly.md  (written only by scripts)
  reports/    daily briefings, plans, regime files, weekly reports
  tests/      offline test suites (no Alpaca needed): test_desk.py and test_commit.sh
```

## 1. Install (once, on the machine that runs your agent)

```bash
cd ~/socrates-desk
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
chmod 444 rules/*.md
git config core.hooksPath .githooks      # turns on the versioned secret-scan pre-commit hook
```

### Secrets (macOS Keychain preferred; a git-ignored .env file is only a fallback)

Run each line, and paste the key when it prompts (nothing shows as you type/paste; that's normal). Use your **paper** Alpaca keys (Alpaca dashboard -> Paper account -> API Keys). Never put the key in the command itself; it would be saved in your shell history.

```bash
security add-generic-password -s socrates-desk -a ALPACA_API_KEY -w
security add-generic-password -s socrates-desk -a ALPACA_SECRET_KEY -w
security add-generic-password -s socrates-desk -a FINNHUB_API_KEY -w     # free at finnhub.io; enables automatic earnings checks
```

Check it worked (prints the first 4 characters only):
```bash
security find-generic-password -s socrates-desk -a ALPACA_API_KEY -w | cut -c1-4
```

To replace a key later, add `-U` to the add command. The Keychain is locked after a reboot until you log in, so turn on automatic login for the Mac Mini's user (System Settings -> Users & Groups) or scheduled runs will fail after a power cut. A `.env` file (KEY=value lines) still works as a fallback on non-Mac machines; it is git-ignored. Keychain items added this way can be read by any program running as the same Mac user, so use paper-only keys (never live keys) for this desk.

## 2. Test before going live

```bash
.venv/bin/python tests/test_desk.py                 # should end: ALL ... CHECKS PASSED
bash tests/test_commit.sh                           # should end: ALL ... COMMIT CHECKS PASSED
.venv/bin/python scripts/place.py --smoke-test      # 1-share SPY order far below market, confirms it, cancels it
.venv/bin/python scripts/score.py --mode briefing   # scores the universe with real data
```

## 3. Daily schedule (trading days, ET)

| Time | Message to Socrates |
|---|---|
| 08:45 | RUN MODE: BRIEFING |
| 10:15 | RUN MODE: DECISION |
| 15:50 | RUN MODE: CLOSE |
| Fri 16:30 | RUN MODE: WEEKLY REVIEW |

The system prompt is `rules/socrates_trading_desk_v1.0.md`. Socrates must always use `.venv/bin/python`, never plain `python`.

## 4. GitHub backup (audit trail)

repo: github.com/cvega9911/socrates-desk. `scripts/commit.sh` runs at the end of every run, so every change is saved with a timestamp. It only ever commits `journal/`, `reports/` and `start_date.txt`, pulls before pushing, and prints `Saved to GitHub.` only when the push really worked (anything else starts with `BACKUP FAILED` and exits non-zero). Before each commit `scripts/secret_scan.sh` checks the staged changes against the real key values in the Keychain (without printing them), anything shaped like an API key, and gitleaks if it is installed. GitHub Actions (`.github/workflows/checks.yml`) runs gitleaks and both test suites on every push.

`trades.jsonl` and `control.jsonl` are rewritten in place when a trade or control changes state (atomically, under a lock). `candidates.jsonl` is append-only. A corrupted line is never dropped silently: it is copied to `journal/corrupt_lines.txt` with a warning.

## 5. Obsidian (reading the desk)

Obsidian -> "Open folder as vault" -> pick `socrates-desk`. No plugin needed on the same machine. On other devices, clone the repo and use the community "Git" plugin set to pull only; do not edit journal files from Obsidian, it would break the audit trail. The mobile Git plugin is experimental; the GitHub app is a reliable way to read the files on a phone.

## 6. The random control group (automatic)

Every DECISION run also draws 5 random trades (random symbol and direction, same entry/stop/target rules, same hard gates, score ignored) into `journal/control.jsonl`. They are never traded. `sync.py` resolves them on split-adjusted daily bars as they hit a target, a stop, or the 10-day time stop. The draw is seeded by date and rules version, so the same day always gives the same draw on the same Python version (logged on each row); the rules version is pinned in the code, so it can't be bumped to reset the scorecard. Stocks whose earnings date couldn't be verified are never drawn.

To draw controls for a day where DECISION already ran without them (only while the market is open): `.venv/bin/python scripts/score.py --mode controls`

## 7. What the Friday review tells you

- **5b Random baseline (the one primary test):** do the planned candidates beat random ones? Both are simulated the same way from the logged levels, so fills and timing can't tilt it. Two-sided block permutation test (shuffled within each ISO week), alpha 0.05, only after 20+ matured planned candidates over 10+ decision days and 50+ matured controls. Real fills are shown alongside, descriptive only.
- **5c Score study (exploratory):** simulates every scored candidate (about 31 a day, not just the 5 traded) for 10 days and shows average R by score and by each factor, including Socrates' regime points. Significance test (shuffled within each day) after 200+ matured candidates over 10+ days. Many tables are read at once with no correction, so a hit here is a lead, not a finding.
- **6 Calibration:** Socrates' p_target is anchored to the neutral base rate 1/(1+R:R); place.py refuses values more than 15 points away, and the review scores him against the base rate on trades that ended at the target or the stop (time-stop exits and pre-rule trades are counted but left out).
- The SPY comparison (section 3) mostly reflects market exposure, not skill. Judge the edge on R and on 5b/5c.

## 8. Rules about the rules

No trading-rule changes until 40 closed A+B trades. A change means: edit the prompt, bump `rules_version.txt` (v1.1), record it in `rules/CHANGELOG.md`, commit. The review compares only trades from the current version. Measurement only changes (logging, reports) are recorded in the changelog but don't change which trades are taken (for continuity purposes).

## 9. Where the rules come from

Rules v1.0 is **not a published or consensus strategy.** It is a rules-based swing-trading design assembled for this experiment from well-studied ideas, with weights and thresholds chosen in advance (not fitted to past data, which avoids overfitting). The point of the desk is to test it, not to assume it works.

| Rule | Based on | Evidence |
|---|---|---|
| Trend factor (0-2) | Time-series momentum: assets with positive past 1-12 month returns tend to keep rising (Moskowitz, Ooi & Pedersen 2012); profitable since 1880 (Hurst, Ooi & Pedersen 2017) | Strong, but measured on diversified futures portfolios held for months |
| Relative strength vs SPY (0-2) | Cross-sectional momentum: past 3-12 month winners beat losers (Jegadeesh & Titman 1993) | Strong at 3-12 month horizons |
| 5-day momentum (0-1) | Common chart practice | **Contradicted:** 1-week and 1-month returns tend to reverse (Lehmann 1990; Jegadeesh 1990). This factor may point the wrong way |
| Volume vs 20-day average (0-1) | Chartist "volume confirms the move" convention | **Weak or opposite:** high-volume stocks earn lower future returns and high-volume winners reverse faster (Lee & Swaminathan 2000) |
| Regime factor (0-2) | Socrates' macro judgment | Untested; that is what the score study measures |
| ATR stops, 1-2 ATR | Wilder (1978), Average True Range | Standard practice, not an edge by itself |
| Fixed % risk sizing, R-multiples | Van Tharp (1998) | Risk management, not an edge |
| Earnings blackout | Avoid earnings-day gaps that jump stops | Standard risk practice |
| Score weights, tiers, quota rule, 10-day hold | Design choices for this experiment | None; chosen to make the test run |
| Random control group, permutation tests | Aronson (2006); White's Reality Check (2000) | Standard method for separating skill from luck |
| Matured-only results, sample-size gates | Multiple-testing and overfitting warnings (Harvey, Liu & Zhu 2016; Bailey et al. 2017) | Standard method |
| p_target base rate 1/(1+R:R), Brier score | Hit probability for a target R vs stop of 1 under a driftless random walk; Brier (1950) | Standard math |

**Known gaps in the evidence**
- Most momentum research uses 1-12 month signals held for months. This desk holds for at most 10 days, which is outside where the evidence is strongest.
- Reviews of technical analysis find many profitable studies but mostly before the early 1990s, with widespread data-snooping problems (Park & Irwin 2007).
- Two factors (5-day momentum, volume) are contradicted or unsupported by research. They stay in v1.0 because the rules were fixed before launch; section 5c of the review tests each factor separately, and any change waits for 40 closed trades and a version bump.

**References**
- Moskowitz, Ooi & Pedersen (2012). "Time Series Momentum." *Journal of Financial Economics* 104(2). [SSRN](https://papers.ssrn.com/abstract=2089463)
- Hurst, Ooi & Pedersen (2017). "A Century of Evidence on Trend-Following Investing." *Journal of Portfolio Management*. [AQR](https://www.aqr.com/Insights/Research/Journal-Article/A-Century-of-Evidence-on-Trend-Following-Investing)
- Jegadeesh & Titman (1993). "Returns to Buying Winners and Selling Losers." *Journal of Finance* 48(1): 65-91. [RePEc](https://ideas.repec.org/a/bla/jfinan/v48y1993i1p65-91.html)
- Jegadeesh (1990). "Evidence of Predictable Behavior of Security Returns." *Journal of Finance* 45(3).
- Lehmann (1990). "Fads, Martingales, and Market Efficiency." *Quarterly Journal of Economics* 105(1). Summary: [Alpha Architect](https://alphaarchitect.com/quantitative-momentum-research-short-term-return-reversal/)
- Lee & Swaminathan (2000). "Price Momentum and Trading Volume." *Journal of Finance* 55(5): 2017-2069. [RePEc](https://ideas.repec.org/a/bla/jfinan/v55y2000i5p2017-2069.html)
- Park & Irwin (2007). "What Do We Know About the Profitability of Technical Analysis?" *Journal of Economic Surveys* 21(4): 786-826. [RePEc](https://ideas.repec.org/a/bla/jecsur/v21y2007i4p786-826.html)
- Harvey, Liu & Zhu (2016). "...and the Cross-Section of Expected Returns." *Review of Financial Studies* 29(1).
- Bailey, Borwein, López de Prado & Zhu (2017). "The Probability of Backtest Overfitting." *Journal of Computational Finance*. [SSRN](https://papers.ssrn.com/abstract=2326253)
- White (2000). "A Reality Check for Data Snooping." *Econometrica* 68(5).
- Aronson (2006). *Evidence-Based Technical Analysis.* Wiley.
- Wilder (1978). *New Concepts in Technical Trading Systems.*
- Van Tharp (1998). *Trade Your Way to Financial Freedom.* McGraw-Hill.
- Brier (1950). "Verification of Forecasts Expressed in Terms of Probability." *Monthly Weather Review* 78(1).
