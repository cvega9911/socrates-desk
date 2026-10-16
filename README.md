# Socrates Desk

A paper-trading experiment run by Socrates (Hermes agent). The scripts do the math and the bookkeeping; Socrates does the research and writes the reasoning.

```
socrates-desk/
  rules/      the system prompt (read-only, v1.0)
  scripts/    score.py, place.py, sync.py, review.py, commit.sh
  journal/    candidates.jsonl, trades.jsonl, weekly.md  (written only by scripts)
  reports/    daily briefings, plans, regime files, weekly reports
  tests/      offline test suite (no Alpaca needed)
```

## 1. Install (once, on the machine that runs Socrates)

Open Terminal and run these one at a time. Put the folder wherever Socrates' workspace is.

```bash
cd ~/socrates-desk
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
chmod 444 rules/*.md
```

### Secrets (macOS Keychain, no .env file)

Run each line, and paste the key when it prompts (nothing shows as you type/paste; that's normal). Use your **paper** Alpaca keys (Alpaca dashboard → Paper account → API Keys).

```bash
security add-generic-password -s socrates-desk -a ALPACA_API_KEY -w
security add-generic-password -s socrates-desk -a ALPACA_SECRET_KEY -w
security add-generic-password -s socrates-desk -a FINNHUB_API_KEY -w     # optional, free at finnhub.io
```

Check it worked (prints the start of the key only):
```bash
security find-generic-password -s socrates-desk -a ALPACA_API_KEY -w | cut -c1-4
```

To replace a key later, add `-U` to the add command. If you ever made a `.env`, delete it: `rm -P .env`.

The Keychain is locked after a reboot until you log in, so turn on automatic login for the Mac Mini's user (System Settings → Users & Groups) or scheduled runs will fail after a power cut. `.env` still works as a fallback on non-Mac machines.

Make sure Socrates runs Python from that `.venv` (or tell him to run `source .venv/bin/activate` first).

## 2. Test before going live

```bash
python tests/test_desk.py                 # should end: ALL 74 CHECKS PASSED
python scripts/place.py --smoke-test      # places a 1-share SPY order far below market, confirms, cancels
python scripts/score.py --mode briefing   # scores the universe with real data
```

If the smoke test passes, the Alpaca connection works and anything that goes wrong later is the prompt, not the plumbing.

## 3. Daily schedule (trading days, ET)

| Time | Message to Socrates |
|---|---|
| 08:45 | RUN MODE: BRIEFING |
| 10:15 | RUN MODE: DECISION |
| 15:50 | RUN MODE: CLOSE |
| Fri 16:30 | RUN MODE: WEEKLY REVIEW |

The system prompt is `rules/socrates_trading_desk_v1.0.md`.

## 4. GitHub backup (audit trail)

1. Install the GitHub command-line tool: `brew install gh` (Mac), then `gh auth login` and follow the prompts (choose GitHub.com, HTTPS, log in with browser).
2. In the desk folder:
```bash
git init
git add -A
git commit -m "desk v1.0"
gh repo create socrates-desk --private --source=. --push
```
3. Done. Socrates runs `scripts/commit.sh` at the end of every run, so every change is saved with a timestamp. If a trade disappears or a rule changes, GitHub's history shows when.

`.env` (your keys) is in `.gitignore` and never uploaded.

## 5. Obsidian (reading the desk)

**On the computer that runs Socrates:** Obsidian → "Open folder as vault" → pick `socrates-desk`. That's it; no plugin needed. Daily reports appear in `reports/` as notes.

**On another computer or your phone:** clone the GitHub repo, open it as a vault, and install the community plugin "Git" (by Vinzent03) to pull updates. Set it to pull on startup and turn OFF auto-commit/push, so you only ever read. Editing journal files from Obsidian would break the audit trail.

On mobile the Git plugin needs a GitHub personal access token (GitHub → Settings → Developer settings → Fine-grained tokens, read access to the socrates-desk repo) and is experimental. If it's flaky, Obsidian Sync (paid) or simply reading the files in the GitHub app is more reliable.

## 6. The random control group (automatic)

Every DECISION run also draws 5 random trades (random symbol and direction, same entry/stop/target rules, same hard gates, score ignored) into `journal/control.jsonl`. They are never traded. `sync.py` resolves them on daily bars as they hit a target, a stop, or the 10-day time stop. The Friday review compares your real trades against them in section 5b and gives a verdict once there are 20+ closed real trades and 50+ resolved controls. The draw is seeded by date and rules version, so it can't be cherry-picked. Nothing to run; Socrates' prompt doesn't change.

To draw controls for a day where DECISION already ran without them (only while the market is open): `.venv/bin/python scripts/score.py --mode controls`

## 7. Rules about the rules

No rule changes until 40 closed A+B trades. A change means: edit the prompt, bump `rules_version.txt` (v1.1), commit. The review script compares only trades from the current version.
