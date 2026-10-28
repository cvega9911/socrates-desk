#!/bin/bash
# Exit 1 if changes about to be committed or pushed contain one of the desk's secrets, anything shaped like an
# API key, or a binary file. It compares against the real key values (Keychain, or .env as a fallback) without
# ever printing them, and it fails CLOSED: if no key values can be loaded, nothing is committed.
#   bash scripts/secret_scan.sh              # staged changes (pre-commit)
#   bash scripts/secret_scan.sh --range REF  # everything between REF and HEAD (pre-push)
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
if [ "${1:-}" = "--range" ] && [ -n "${2:-}" ]; then
  diff=$(git diff -U0 --no-color --no-ext-diff "$2" HEAD) || exit 2
  names=$(git diff --name-only "$2" HEAD) || exit 2
  numstat=$(git diff --numstat "$2" HEAD) || exit 2
else
  diff=$(git diff --cached -U0 --no-color --no-ext-diff) || exit 2
  names=$(git diff --cached --name-only) || exit 2
  numstat=$(git diff --cached --numstat) || exit 2
fi
[ -z "$diff" ] && [ -z "$names" ] && exit 0
added=$(printf '%s\n' "$diff" | grep -E '^\+' | grep -vE '^\+\+\+ ')
haystack=$(printf '%s\n%s\n' "$added" "$names")
found=0

if printf '%s\n' "$numstat" | grep -qE '^-[[:space:]]+-[[:space:]]'; then
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
    if printf '%s' "$haystack" | grep -qF -- "$val"; then
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
if printf '%s' "$haystack" | grep -qE '(^|[^A-Za-z0-9])(PK|AK|CK)[A-Z0-9]{16,20}([^A-Za-z0-9]|$)|APCA-API-(KEY-ID|SECRET-KEY)|(api[_-]?key|secret[_-]?key|access[_-]?token|token)["'"'"' ]*[:=]["'"'"' ]*[A-Za-z0-9/+_-]{20,}'; then
  echo "SECRET SCAN: the changes contain something shaped like an API key." >&2
  found=1
fi
if command -v gitleaks >/dev/null 2>&1; then
  out=$(gitleaks protect --staged --redact --no-banner 2>&1)
  if [ $? -ne 0 ] && printf '%s' "$out" | grep -qiE 'leaks found: [1-9]'; then
    echo "SECRET SCAN: gitleaks flagged the changes." >&2
    found=1
  fi
fi
exit $found
