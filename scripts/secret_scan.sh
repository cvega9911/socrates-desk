#!/bin/bash
# Exit 1 if changes about to be committed or pushed contain one of the desk's secrets, anything shaped like an
# API key, or a binary file. It compares against the real key values (Keychain, or .env as a fallback) without
# ever printing them, and it fails CLOSED: if no key values can be loaded, nothing is committed.
#   bash scripts/secret_scan.sh              # staged changes (pre-commit)
#   bash scripts/secret_scan.sh --range REF  # EVERY commit between REF and HEAD, one by one, merge commits
#                                            # included (pre-push), so a key added in one commit and deleted in
#                                            # the next, or slipped in while resolving a merge, is still caught
# Matching uses here-strings, never `printf | grep -q` (with pipefail, grep -q's early exit makes printf die of
# SIGPIPE on big inputs and the match would be lost). Works on macOS bash 3.2.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
if [ "${1:-}" = "--range" ] && [ -n "${2:-}" ]; then
  diff=$(git log -m -p -U0 --no-color --no-ext-diff --format= "$2..HEAD") || exit 2
  names=$(git log -m --name-only --format= "$2..HEAD") || exit 2
  numstat=$(git log -m --numstat --format= "$2..HEAD") || exit 2
else
  diff=$(git diff --cached -U0 --no-color --no-ext-diff) || exit 2
  names=$(git diff --cached --name-only) || exit 2
  numstat=$(git diff --cached --numstat) || exit 2
fi
[ -z "$diff" ] && [ -z "$names" ] && exit 0
added=$(grep -E '^\+' <<<"$diff" | grep -vE '^\+\+\+ ')
haystack="$added
$names"
found=0

if grep -qE '^-[[:space:]]+-[[:space:]]' <<<"$numstat"; then
  echo "SECRET SCAN: a binary file is included; the desk only ever saves text. Not committing." >&2
  found=1
fi

keychain() {   # never hangs: macOS has no `timeout`, so use perl's alarm
  command -v security >/dev/null 2>&1 || return 0
  perl -e 'alarm 10; exec @ARGV' security find-generic-password -s socrates-desk -a "$1" -w 2>/dev/null || true
}
loaded=0
for name in ALPACA_API_KEY ALPACA_SECRET_KEY FINNHUB_API_KEY; do
  val=$(keychain "$name")
  if [ -z "$val" ] && [ -f .env ]; then
    val=$(grep -E "^[[:space:]]*${name}[[:space:]]*=" .env | tail -1 | cut -d= -f2- | tr -d "\"' \r")
  fi
  if [ ${#val} -ge 8 ]; then
    loaded=$((loaded + 1))
    if grep -qF -- "$val" <<<"$haystack"; then
      echo "SECRET SCAN: the changes contain the value of $name." >&2
      found=1
    fi
  fi
  unset val
done
if [ "$loaded" -eq 0 ]; then
  echo "SECRET SCAN: could not load any key values to compare against (Keychain locked?). Not committing." >&2
  found=1
fi
if grep -qE '(^|[^A-Za-z0-9])(PK|AK|CK)[A-Z0-9]{16,20}([^A-Za-z0-9]|$)|APCA-API-(KEY-ID|SECRET-KEY)|(api[_-]?key|secret[_-]?key|access[_-]?token|token)["'"'"' ]*[:=]["'"'"' ]*[A-Za-z0-9/+_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY-----' <<<"$haystack"; then
  echo "SECRET SCAN: the changes contain something shaped like an API key or private key." >&2
  found=1
fi
if command -v gitleaks >/dev/null 2>&1; then
  if [ "${1:-}" = "--range" ]; then
    out=$(gitleaks detect --log-opts="$2..HEAD" --redact --no-banner 2>&1)
  else
    out=$(gitleaks protect --staged --redact --no-banner 2>&1)
  fi
  if [ $? -ne 0 ] && grep -qiE 'leaks found: [1-9]' <<<"$out"; then
    echo "SECRET SCAN: gitleaks flagged the changes." >&2
    found=1
  fi
fi
exit $found
