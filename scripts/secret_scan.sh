#!/bin/bash
# Exit 1 if the staged changes contain one of the desk's secrets or anything shaped like an API key.
# It compares against the real key values from the Keychain (or .env) without ever printing them.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
diff=$(git diff --cached -U0 --no-color --no-ext-diff) || exit 2
[ -z "$diff" ] && exit 0
added=$(printf '%s\n' "$diff" | grep -E '^\+' | grep -vE '^\+\+\+ ')
found=0
for name in ALPACA_API_KEY ALPACA_SECRET_KEY FINNHUB_API_KEY; do
  val=""
  if command -v security >/dev/null 2>&1; then
    val=$(security find-generic-password -s socrates-desk -a "$name" -w 2>/dev/null || true)
  fi
  if [ -z "$val" ] && [ -f .env ]; then
    val=$(grep -E "^[[:space:]]*${name}[[:space:]]*=" .env | tail -1 | cut -d= -f2- | tr -d "\"' \r")
  fi
  if [ ${#val} -ge 8 ] && printf '%s' "$added" | grep -qF -- "$val"; then
    echo "SECRET SCAN: the staged changes contain the value of $name." >&2
    found=1
  fi
  unset val
done
if printf '%s' "$added" | grep -qE '(^|[^A-Za-z0-9])(PK|AK|CK)[A-Z0-9]{16,20}([^A-Za-z0-9]|$)|APCA-API-(KEY-ID|SECRET-KEY)|(api[_-]?key|secret[_-]?key|access[_-]?token)["'"'"' ]*[:=]["'"'"' ]*[A-Za-z0-9/+_-]{20,}'; then
  echo "SECRET SCAN: the staged changes contain something shaped like an API key." >&2
  found=1
fi
if command -v gitleaks >/dev/null 2>&1; then
  out=$(gitleaks protect --staged --redact --no-banner 2>&1)
  if [ $? -ne 0 ] && printf '%s' "$out" | grep -qiE 'leaks found: [1-9]'; then
    echo "SECRET SCAN: gitleaks flagged the staged changes." >&2
    found=1
  fi
fi
exit $found
