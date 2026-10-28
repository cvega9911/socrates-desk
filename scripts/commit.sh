#!/bin/bash
# Save the desk to GitHub. Socrates runs this at the end of every run.
#
# Prints "Saved to GitHub." ONLY when everything worked. Any problem prints a line starting with
# "BACKUP FAILED" and exits non-zero, so it can never fail silently. A run only ever commits journal/,
# reports/ and start_date.txt; code and rules changes go through Chris.
set -uo pipefail
cd "$(dirname "$0")/.." || { echo "BACKUP FAILED: desk folder not found. Tell Chris."; exit 1; }

fail() { echo "BACKUP FAILED: $1 The files are still saved on this Mac. Tell Chris."; exit 1; }

# 1. A run never changes code or rules. If something did, say so loudly (it is not committed here).
changed=$(git status --porcelain -- scripts rules rules_version.txt tests README.md requirements.txt .gitignore .githooks .github 2>/dev/null)
if [ -n "$changed" ]; then
  echo "WARNING: code or rules files were changed on this Mac and were NOT backed up:"
  printf '%s\n' "$changed" | sed 's/^/  /'
  echo "Socrates must never edit these. Tell Chris."
fi

# 2. Stage data only, and unstage anything else someone staged by hand.
paths=()
for p in journal reports start_date.txt; do [ -e "$p" ] && paths+=("$p"); done
[ ${#paths[@]} -gt 0 ] && { git add -A -- "${paths[@]}" || fail "could not stage the journal."; }
git diff --cached --name-only -z -- . ':(exclude)journal' ':(exclude)reports' ':(exclude)start_date.txt' |
  while IFS= read -r -d '' f; do git reset -q -- "$f"; done

# 3. Never let a key reach a public repo.
bash scripts/secret_scan.sh || fail "the secret scan blocked the commit (see above). Nothing was committed."

# 4. Commit if anything changed.
if ! git diff --cached --quiet; then
  git commit -qm "desk: $(date '+%Y-%m-%d %H:%M')" || fail "git commit failed (a hook blocked it?)."
fi

# 5. Bring in anything changed on GitHub first, then push. Earlier unpushed commits go up too.
if ! out=$(git pull -q --rebase --autostash 2>&1); then
  git rebase --abort >/dev/null 2>&1
  fail "could not combine with changes on GitHub: $(printf '%s' "$out" | tail -2 | tr '\n' ' ')"
fi
if ! out=$(git push -q 2>&1); then
  fail "push to GitHub failed: $(printf '%s' "$out" | tail -2 | tr '\n' ' ')"
fi
echo "Saved to GitHub."
