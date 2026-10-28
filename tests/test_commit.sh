#!/bin/bash
# Tests for scripts/commit.sh and scripts/secret_scan.sh against a real (local) git remote.
# Run: bash tests/test_commit.sh
set -uo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
pass=0
ok() { if eval "$1"; then pass=$((pass+1)); echo "  ok - $2"; else echo "  FAIL - $2"; exit 1; fi; }

export GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=t@t
export GIT_MERGE_AUTOEDIT=no GIT_EDITOR=true    # never open an editor mid-test (a terminal would wait for input)
export PATH="/usr/bin:/bin:$PATH"
git init -q --bare -b main "$T/remote.git"
git clone -q "$T/remote.git" "$T/desk" 2>/dev/null
cd "$T/desk"
mkdir -p scripts journal reports
cp "$SRC/scripts/commit.sh" "$SRC/scripts/secret_scan.sh" scripts/
echo "v1.0" > rules_version.txt
touch journal/trades.jsonl
printf 'ALPACA_API_KEY=PKZZTESTKEYVALUE1234\nALPACA_SECRET_KEY=zzSecretValueForTests0123456789abcdefgh\nFINNHUB_API_KEY=zzfinnhubtesttoken99\n' > .env
echo ".env" > .gitignore
git add -A && git commit -qm init && git push -q -u origin main 2>/dev/null

echo "commit.sh"
echo '{"a":1}' >> journal/trades.jsonl
out=$(bash scripts/commit.sh); rc=$?
ok '[ $rc -eq 0 ] && [ "$out" = "Saved to GitHub." ]' "saves new journal rows and says so"
ok '[ "$(git --git-dir="$T/remote.git" log --oneline | wc -l | tr -d " ")" = "2" ]' "the commit reached the remote"

out=$(bash scripts/commit.sh); rc=$?
ok '[ $rc -eq 0 ] && [ "$out" = "Saved to GitHub." ]' "nothing new: still succeeds"

git clone -q "$T/remote.git" "$T/other" 2>/dev/null
(cd "$T/other" && mkdir -p reports && echo '{"x":0}' > reports/other.json && git add reports && git commit -qm "data from elsewhere" && git push -q 2>/dev/null)
echo '{"b":2}' >> journal/trades.jsonl
out=$(bash scripts/commit.sh); rc=$?
ok '[ $rc -eq 0 ] && [ "$out" = "Saved to GitHub." ] && [ -f reports/other.json ]' "data changed on GitHub: rebases and pushes instead of failing forever"

(cd "$T/other" && git pull -q 2>/dev/null && echo 'echo pwned' >> scripts/commit.sh && git commit -qam "edit code on github" && git push -q 2>/dev/null)
echo '{"b2":2}' >> journal/trades.jsonl
out=$(bash scripts/commit.sh 2>&1); rc=$?
ok '[ $rc -ne 0 ] && printf "%s" "$out" | grep -q "code or rules changes" && ! grep -q pwned scripts/commit.sh' "code changed on GitHub is never pulled into a run (Chris pulls it by hand)"
git pull -q --no-rebase 2>/dev/null; git checkout -q -- . ; git reset -q --hard origin/main; sed -i.bak '/echo pwned/d' scripts/commit.sh && rm -f scripts/commit.sh.bak && git commit -qam "Chris reviewed and reverted" && git push -q 2>/dev/null

(cd "$T/other" && git pull -q 2>/dev/null && echo '{"conflict":1}' >> journal/trades.jsonl && git commit -qam "conflict" && git push -q 2>/dev/null)
echo '{"c":3}' >> journal/trades.jsonl
out=$(bash scripts/commit.sh 2>&1); rc=$?
ok '[ $rc -ne 0 ] && printf "%s" "$out" | grep -q "BACKUP FAILED"' "conflicting change on GitHub: loud BACKUP FAILED, non-zero exit"
ok '[ ! -d .git/rebase-merge ] && [ ! -d .git/rebase-apply ]' "a failed rebase is aborted (repo not left mid-rebase)"
ok 'git log -1 --format=%s | grep -q "^desk:"' "the local commit is kept for the next try"
git reset -q --hard origin/main && git pull -q 2>/dev/null

echo '{"d":4}' >> journal/trades.jsonl
echo "leaked PKZZTESTKEYVALUE1234 here" > reports/oops.md
out=$(bash scripts/commit.sh 2>&1); rc=$?
ok '[ $rc -ne 0 ] && printf "%s" "$out" | grep -q "BACKUP FAILED" && printf "%s" "$out" | grep -q "ALPACA_API_KEY"' "a real key value in a report blocks the commit"
ok '! printf "%s" "$out" | grep -q PKZZTESTKEYVALUE1234' "the key value itself is never printed"
ok '! git log --all -p | grep -q PKZZTESTKEYVALUE1234 && ! git --git-dir="$T/remote.git" log --all -p | grep -q PKZZTESTKEYVALUE1234' "the key never entered any commit, locally or on the remote"
rm -f reports/oops.md
git reset -q

echo 'secret_key = "abcdefghijklmnopqrstuvwxyz123456"' > reports/shape.md
out=$(bash scripts/commit.sh 2>&1); rc=$?
ok '[ $rc -ne 0 ] && printf "%s" "$out" | grep -q "shaped like an API key"' "something shaped like a key blocks the commit"
rm -f reports/shape.md; git reset -q

printf 'zzSecretValueFor\nTests0123' > reports/split.md
printf 'PNG\000\001\002binary\000data' > reports/blob.bin
out=$(bash scripts/commit.sh 2>&1); rc=$?
ok '[ $rc -ne 0 ] && printf "%s" "$out" | grep -q "binary file"' "a binary file in reports/ blocks the commit"
rm -f reports/blob.bin reports/split.md; git reset -q

mv .env .env.off
echo '{"nokeys":1}' >> journal/trades.jsonl
out=$(bash scripts/commit.sh 2>&1); rc=$?
ok '[ $rc -ne 0 ] && printf "%s" "$out" | grep -q "could not load any key values"' "the scan fails closed when it can't load the real key values"
mv .env.off .env; git reset -q

touch "reports/PKZZTESTKEYVALUE1234.md"
out=$(bash scripts/commit.sh 2>&1); rc=$?
ok '[ $rc -ne 0 ] && printf "%s" "$out" | grep -q "ALPACA_API_KEY"' "a key hidden in a file NAME is caught too"
rm -f "reports/PKZZTESTKEYVALUE1234.md"; git reset -q

git commit -q --no-verify --allow-empty -m "sneaky" && echo "PKZZTESTKEYVALUE1234" > journal/sneak.txt && git add journal/sneak.txt && git commit -q --no-verify -m "unscanned commit with a key"
echo '{"h":8}' >> journal/trades.jsonl
out=$(bash scripts/commit.sh 2>&1); rc=$?
ok '[ $rc -ne 0 ] && ! git --git-dir="$T/remote.git" log --all -p | grep -q PKZZTESTKEYVALUE1234' "a key in an earlier unpushed commit is caught before the push"
git reset -q --hard origin/main

mkdir -p .git/hooks && printf '#!/bin/sh\nexit 1\n' > .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit
echo '{"e":5}' >> journal/trades.jsonl
out=$(bash scripts/commit.sh 2>&1); rc=$?
ok '[ $rc -ne 0 ] && printf "%s" "$out" | grep -q "BACKUP FAILED: git commit failed"' "a blocking hook is reported, not 'Saved'"
rm -f .git/hooks/pre-commit

echo "x" > rules_version.txt
echo '{"t":1}' >> journal/trades.jsonl
out=$(bash scripts/commit.sh 2>&1); rc=$?
ok '[ $rc -ne 0 ] && printf "%s" "$out" | grep -q "own code or rules were edited"' "edited rules/code stop the backup (the edited code can't be trusted)"
ok '! git show --stat HEAD | grep -q rules_version.txt' "edited rules/code are not committed by a run"
git checkout -q -- rules_version.txt
printf 'exit 0\n' | cat - scripts/secret_scan.sh > /tmp/ss.$$ && cp /tmp/ss.$$ scripts/secret_scan.sh && rm -f /tmp/ss.$$
echo "PKZZTESTKEYVALUE1234" > reports/k.md
out=$(bash scripts/commit.sh 2>&1); rc=$?
ok '[ $rc -ne 0 ] && ! git --git-dir="$T/remote.git" log --all -p | grep -q PKZZTESTKEYVALUE1234' "a scan disabled by editing it can't push a key"
git checkout -q -- scripts/secret_scan.sh; rm -f reports/k.md; git reset -q; git checkout -q -- journal 2>/dev/null

python3 - <<'PY'
import random
random.seed(1)
with open("journal/aaa_big.txt", "w") as f:
    f.write("PKZZTESTKEYVALUE1234\n")
    for i in range(6000):
        f.write("".join(random.choice("abcdefghij ") for _ in range(60)) + "\n")
PY
out=$(bash scripts/commit.sh 2>&1); rc=$?
ok '[ $rc -ne 0 ] && ! git --git-dir="$T/remote.git" log --all -p | grep -q PKZZTESTKEYVALUE1234' "a key at the start of a ~360 KB diff is still caught (no SIGPIPE fail-open)"
rm -f journal/aaa_big.txt; git reset -q

git commit -q --no-verify --allow-empty -m "noop"
echo "PKZZTESTKEYVALUE1234" > journal/tmpkey.txt && git add journal/tmpkey.txt && git commit -q --no-verify -m "add key"
git rm -q journal/tmpkey.txt && git commit -q --no-verify -m "remove key"
out=$(bash scripts/commit.sh 2>&1); rc=$?
ok '[ $rc -ne 0 ] && ! git --git-dir="$T/remote.git" log --all -p | grep -q PKZZTESTKEYVALUE1234' "a key added then deleted in unpushed commits is still caught (scanned commit by commit)"
git reset -q --hard origin/main

git checkout -q -b side && echo '{"s":1}' > reports/side.json && git add reports && git commit -q --no-verify -m side
git checkout -q main && echo '{"m":1}' > reports/main.json && git add reports && git commit -q --no-verify -m main-side
git merge -q --no-commit --no-ff side 2>/dev/null; echo "PKZZTESTKEYVALUE1234" > reports/merged.txt && git add reports
git commit -q --no-verify -m "merge with a key slipped in"
out=$(bash scripts/secret_scan.sh --range origin/main 2>&1); rc=$?
ok '[ $rc -ne 0 ] && ! printf "%s" "$out" | grep -q PKZZTESTKEYVALUE1234' "a key added while resolving a merge is caught by the push scan"
git reset -q --hard origin/main; git branch -q -D side

echo "junk" > notes.txt && git add notes.txt
echo '{"f":6}' >> journal/trades.jsonl
out=$(bash scripts/commit.sh 2>&1); rc=$?
ok '[ $rc -eq 0 ] && ! git show --stat HEAD | grep -q notes.txt' "a file staged by hand outside journal/reports is not committed"
rm -f notes.txt; git reset -q

git remote set-url origin "$T/nowhere.git"
echo '{"g":7}' >> journal/trades.jsonl
out=$(bash scripts/commit.sh 2>&1); rc=$?
ok '[ $rc -ne 0 ] && printf "%s" "$out" | grep -q "BACKUP FAILED"' "unreachable GitHub: loud failure, not 'Saved'"

echo
echo "ALL $pass COMMIT CHECKS PASSED"
