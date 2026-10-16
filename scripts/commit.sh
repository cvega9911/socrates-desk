#!/bin/bash
# Save the desk to GitHub. Socrates runs this at the end of every run.
cd "$(dirname "$0")/.." || exit 1
git add -A
git diff --cached --quiet || git commit -qm "desk: $(date '+%Y-%m-%d %H:%M')"
git push -q 2>/dev/null && echo "Saved to GitHub." || echo "GitHub push failed (check internet or login). Files are still saved locally."
