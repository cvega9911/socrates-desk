# Socrates Desk

A paper-trading research experiment run by Socrates (a locally deployed Hermes agent framework running on Qwen 3.8 27b). The scripts do the math and the bookkeeping; Socrates does the research and writes the reasoning.

```
socrates-desk/
  rules/      the system prompt (v1.0, read-only) + CHANGELOG.md
  scripts/    score.py, place.py, sync.py, review.py, control.py, commit.sh (+ broker.py, desk.py helpers)
  journal/    candidates.jsonl, trades.jsonl, control.jsonl, weekly.md  (written only by scripts)
  reports/    daily briefings, plans, regime files, weekly reports
  tests/      offline test suite (no Alpaca needed)
```

## 1. Install (once, on the machine that runs your agent)

```bash
cd ~/socrates-desk
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
chmod 444 rules/*.md
```

### Secrets (macOS Keychain or comparable, no .env file)

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

To replace a key later, add `-U` to the add command. The Keychain is locked after a reboot until you log in, so turn on automatic login for the Mac Mini's user (System Settings -> Users & Groups) or scheduled runs will fail after a power cut. A `.env` file (KEY=value lines) still works as a fallback on non-Mac machines; it is git-ignored.

## 2. Test before going live

```bash
.venv/bin/python tests/test_desk.py                 # should end: ALL 84 CHECKS PASSED
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

Private repo: github.com/cvega9911/socrates-desk. `scripts/commit.sh` runs at the end of every run, so every change is saved with a timestamp. A gitleaks pre-commit hook blocks any commit that contains something shaped like a key.

## 5. Obsidian (reading the desk)

Obsidian -> "Open folder as vault" -> pick `socrates-desk`. No plugin needed on the same machine. On other devices, clone the repo and use the community "Git" plugin set to pull only; do not edit journal files from Obsidian, it would break the audit trail. The mobile Git plugin is experimental; the GitHub app is a reliable way to read the files on a phone.

## 6. The random control group (automatic)

Every DECISION run also draws 5 random trades (random symbol and direction, same entry/stop/target rules, same hard gates, score ignored) into `journal/control.jsonl`. They are never traded. `sync.py` resolves them on daily bars as they hit a target, a stop, or the 10-day time stop. The draw is seeded by date and rules version, so it can't be cherry-picked. Nothing to run; Socrates' prompt doesn't change.

To draw controls for a day where DECISION already ran without them (only while the market is open): `.venv/bin/python scripts/score.py --mode controls`

## 7. What the Friday review tells you

- **5b Random baseline:** do real trades beat random ones? Verdict only from trades/controls opened 15+ days ago (so early exits don't skew it), after 20+ matured real trades and 50+ matured controls.
- **5c Score study:** simulates every scored candidate (about 31 a day, not just the 5 traded) for 10 days and shows average R by score and by each factor, including Socrates' regime points. Significance test (shuffled within each day) after 200+ matured candidates over 10+ days.
- **6 Calibration:** Socrates' p_target is anchored to the neutral base rate 1/(1+R:R); place.py refuses values more than 15 points away, and the review scores him against the base rate.
- The SPY comparison (section 3) mostly reflects market exposure, not skill. Judge the edge on R and on 5b/5c.

## 8. Rules about the rules

No trading-rule changes until 40 closed A+B trades. A change means: edit the prompt, bump `rules_version.txt` (v1.1), record it in `rules/CHANGELOG.md`, commit. The review compares only trades from the current version. Measurement-only changes (logging, reports) are recorded in the changelog but don't change which trades are taken.
